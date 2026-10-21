// Modelo de vehiculo para el port web de FlatOut UC.
//
// Usa los parametros reales del juego (tools/gamedb.py -> web/cars/car_N/physics.json):
//   masa / centro de masa, motor (par y potencia maximos, regimenes), caja (relaciones, par final),
//   suspension (longitudes, compresion estatica, amortiguacion, barra estabilizadora), neumaticos
//   (radio, masa, inercia, friccion, deslizamiento optimo), frenos y direccion por velocidad.
//
// Rapier solo integra el cuerpo rigido y lanza rayos; las fuerzas de suspension, neumaticos,
// motor y aerodinamica se calculan aqui y se aplican como impulsos en cada paso.
//
// Convenciones (three.js): +x derecha, +y arriba, el coche mira hacia -z.
// Interpretaciones propias (el juego no las documenta): la 1a componente de los pares [min, max]
// es el coche de serie; las amortiguaciones de suspension son fracciones del valor critico;
// AeroDrag/DownforceConst son coeficientes de F = c * v^2.
import * as THREE from 'three';

const G = 9.81;
const RPM_TO_RAD = Math.PI / 30;
const first = v => (Array.isArray(v) ? v[0] : v);
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));

function curve(points, x) {
  if (x <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    if (x <= points[i][0]) {
      const [x0, y0] = points[i - 1], [x1, y1] = points[i];
      return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
    }
  }
  return points[points.length - 1][1];
}

// Curva de par del motor reconstruida a partir de los puntos que da el juego.
function engineCurve(e) {
  const idle = first(e.IdleRpm), red = first(e.RedLineRpm);
  const rpmT = first(e.PeakTorqueRpm), tPeak = first(e.PeakTorque);
  const rpmP = Math.max(first(e.PeakPowerRpm), rpmT + 200), pPeak = first(e.PeakPower) * 1000;
  const tAtP = pPeak / (rpmP * RPM_TO_RAD);
  return [[0, 0], [idle * 0.6, tPeak * 0.4], [idle, tPeak * 0.65], [rpmT, tPeak], [rpmP, tAtP],
          [red, tAtP * 0.88], [red * 1.05, tAtP * 0.3]];
}

export class Vehicle {
  /**
   * @param RAPIER   modulo de Rapier ya inicializado
   * @param world    RAPIER.World
   * @param rb       cuerpo rigido del chasis (masa y centro de masa ya definidos)
   * @param P        contenido de physics.json
   * @param anchors  {fl, fr, rl, rr}: centro de rueda en reposo, coordenadas locales del chasis
   * @param radius   radio del neumatico (m)
   * @param mass     masa total (kg); rb.mass() vale 0 hasta el primer paso de Rapier
   * @param com      centro de masa en coordenadas locales del chasis {x, y, z}
   */
  constructor(RAPIER, world, rb, P, anchors, radius, mass, com) {
    this.RAPIER = RAPIER; this.world = world; this.rb = rb; this.P = P; this.R = radius;
    const body = P.Body, eng = P.Engine, gb = P.Gearbox, tires = P.Tires, st = P.Steering;

    this.mass = mass;
    const zF = (anchors.fl.z + anchors.fr.z) / 2, zR = (anchors.rl.z + anchors.rr.z) / 2;
    const wheelbase = zR - zF;                              // el frente es -z
    const frontShare = clamp((zR - com.z) / wheelbase, 0.3, 0.7);

    // ---- motor y caja ----
    this.torque = engineCurve(eng);
    this.idle = first(eng.IdleRpm); this.red = first(eng.RedLineRpm);
    this.rpmTorque = first(eng.PeakTorqueRpm); this.rpmPower = first(eng.PeakPowerRpm);
    this.tPeak = first(eng.PeakTorque);
    this.numGears = gb.NumGears;
    this.ratios = [];                                       // ratios[g] para g = 1..N
    for (let g = 1; g <= this.numGears; g++) this.ratios[g] = gb['Gear' + g][0];
    this.reverseRatio = gb.GearR[0];                        // negativa
    this.finalDrive = first(gb.EndRatio);
    this.clutchTorque = first(gb.ClutchTorque);
    this.drivetrainEff = 0.92;
    this.front = !!body.FrontTraction; this.rear = !!body.RearTraction;

    // ---- frenos, aerodinamica, direccion ----
    this.brakeTorque = first(body.BrakeTorque); this.brakeBalance = first(body.BrakeBalance);
    this.handbrakeTorque = first(body.HandBrakeTorque);
    this.aeroDrag = first(body.AeroDrag); this.downforce = first(body.DownforceConst);
    this.aeroDragLoc = new THREE.Vector3(body.AeroDragLoc[0], body.AeroDragLoc[1], -body.AeroDragLoc[2]);
    this.maxSteer = THREE.MathUtils.degToRad((body.TireTurnAngleIn + body.TireTurnAngleOut) / 2);
    this.steerLimit = st.SteeringLimitSpeed.map((s, i) => [s, st.SteeringLimitRate[i]]);   // km/h -> fraccion
    this.steerRate = 0.5 * (st.MinDigitalSpeed + st.MaxDigitalSpeed);
    this.centering = st.CenteringSpeed;

    // ---- neumaticos ----
    this.muX = tires.XFriction[0]; this.muZ = tires.ZFriction[0];   // [friccion, sensibilidad a la carga]; X lateral, Z longitudinal
    this.optSlipRatio = tires.OptimalSlipRatio;
    this.optSlipAngle = Math.tan(THREE.MathUtils.degToRad(tires.OptimalSlipAngle));
    this.rollResCar = tires.RollingResistance;   // factor del coche; el valor base lo da la dinamica del suelo (0.005)
    this.antiSpin = first(body.AntiSpinMultiplier);
    this.slideControl = first(body.SlideControlMultiplier);   // ayuda de estabilidad (yaw); 0 en la mayoria de coches de serie
    this.wheelbase = wheelbase;
    this.anchorFrontZ = zF;                                    // z local del eje delantero (el frente es -z)
    this.yawInertia = this.mass * 1.4;                         // aproximacion: m * r^2 con r ~ 1.2 m
    // nitro: NitroStorage = segundos de deposito; NitroAcceleration = aumento relativo del par
    this.nitroMax = first(eng.NitroStorage);
    this.nitroBoost = first(eng.NitroAcceleration);
    this.nitro = this.nitroMax;                                // empieza lleno
    this.nitroActive = false;
    // Superficies: quien conozca la pista rellena surfaceAt(hit) -> {dyn, ...} (hit = impacto del rayo,
    // con hit.collider) y defaultDyn (dinamica de neumatico para suelo desconocido, normalmente Tarmac).
    // dyn = un grupo de TireDynamics de game.db: XFriction/ZFriction, XStiffness/ZStiffness,
    // FrictionBoost, SlowDown, AntiSpin, SlideControl, RollingResistance...
    this.surfaceAt = null;
    this.defaultDyn = null;
    this._surfAssist = 0;
    // Ajustes de "sensacion" (modificables en vivo desde drive.html). Los datos del juego dan el modelo
    // base; estos factores lo acercan al comportamiento arcade del original.
    this.tune = {
      grip: 1.0,          // multiplica la friccion (la del juego ya es ~1.6 en asfalto, ~1.4 en grava)
      slipAngle: 0.55,    // escala el angulo de deriva optimo: menor = el neumatico agarra antes (mas rigido)
      steer: 1.0,         // multiplica el giro maximo del volante
      relax: 0.15,        // longitud de relajacion lateral (m): el neumatico tarda en generar fuerza lateral
      assist: 0.0,        // ayuda de estabilidad extra para todos los coches (ademas de la del coche y la del suelo)
      surface: 1.0,       // cuanto influye el tipo de suelo (0 = todo se comporta como asfalto)
    };

    // ---- ruedas ----
    this.wheels = ['fl', 'fr', 'rl', 'rr'].map(id => {
      const front = id[0] === 'f', S = P.Suspension, pre = front ? 'Front' : 'Rear';
      const rest = first(S[pre + 'RestLength']), minL = first(S[pre + 'MinLength']), maxL = first(S[pre + 'MaxLength']);
      const dc = first(S[pre + 'DefaultCompression']);
      const staticLoad = this.mass * G * (front ? frontShare : 1 - frontShare) / 2;
      const k = staticLoad / dc;                            // N/m: equilibrio con la compresion por defecto
      const cCrit = 2 * Math.sqrt(k * staticLoad / G);
      return {
        id, front, left: anchors[id].x < 0, anchor: new THREE.Vector3(anchors[id].x, anchors[id].y, anchors[id].z),
        rest, minL, maxL, k, staticLoad,
        cBump: first(S[pre + 'BumpDamp']) * cCrit, cReb: first(S[pre + 'ReboundDamp']) * cCrit,
        rollbar: first(S[pre + 'RollbarStiffness']),
        inertia: tires[pre + 'MomentOfInertia'],
        L: rest - dc, prevL: rest - dc, omega: 0, spin: 0, steer: 0, contact: false, Fz: 0,
        slipRatio: 0, slipAngle: 0, syFiltered: 0,
      };
    });

    // ---- perfil de muestreo del neumatico (distancias s en m, a lo largo del avance de la rueda) ----
    this.profile = [-0.26, -0.17, -0.09, 0, 0.09, 0.17, 0.26].map(s => Math.sign(s) * Math.min(Math.abs(s), radius * 0.9));

    // ---- estado ----
    this.gear = 1; this.rpm = this.idle; this.shiftTimer = 0; this.shiftHold = 0; this.steerInput = 0;
    this.speedKmh = 0; this.forwardSpeed = 0;
    this._q = new THREE.Quaternion();
  }

  reset() {
    this.gear = 1; this.rpm = this.idle; this.shiftTimer = 0; this.shiftHold = 0; this.steerInput = 0;
    this.nitro = this.nitroMax;
    for (const w of this.wheels) { w.omega = 0; w.L = w.prevL = w.rest; w.steer = 0; w.syFiltered = 0; }
  }

  // ---------- un paso de fisica: llamar antes de world.step() ----------
  update(dt, input) {
    const { rb, RAPIER, R } = this;
    const t = rb.translation(), r = rb.rotation(), lv = rb.linvel(), av = rb.angvel(), wc = rb.worldCom();
    const q = this._q.set(r.x, r.y, r.z, r.w);
    const pos = new THREE.Vector3(t.x, t.y, t.z), lin = new THREE.Vector3(lv.x, lv.y, lv.z);
    const ang = new THREE.Vector3(av.x, av.y, av.z), com = new THREE.Vector3(wc.x, wc.y, wc.z);
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(q);
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(q);
    this.forwardSpeed = lin.dot(fwd);
    this.speedKmh = Math.abs(this.forwardSpeed) * 3.6;

    // ---- direccion: entrada suavizada y limitada segun la velocidad ----
    const target = input.steer, rate = this.steerRate;
    const returning = target === 0 || Math.sign(target) !== Math.sign(this.steerInput);
    const maxStep = (returning ? rate * Math.max(1, this.centering) : rate) * dt;
    this.steerInput += clamp(target - this.steerInput, -maxStep, maxStep);
    const tune = this.tune;
    const optAngle = this.optSlipAngle * tune.slipAngle;            // tan del angulo de deriva optimo efectivo
    const assist = Math.max(this.slideControl, tune.assist, this._surfAssist);   // ayuda de estabilidad efectiva (coche, ajuste y suelo)
    let delta = this.steerInput * curve(this.steerLimit, this.speedKmh) * this.maxSteer * tune.steer;
    // ayuda de dirección (SlideControl): no deja meter mas volante del que el neumatico delantero
    // puede aprovechar. El angulo de deriva delantero es delta - atan(v_lateral_eje / vx); se acota
    // para que no pase del optimo del neumatico y la rueda no patine ni frene el coche.
    if (assist > 0 && this.forwardSpeed > 6) {
      const right = new THREE.Vector3(1, 0, 0).applyQuaternion(q);
      const vLeft = -lin.dot(right), yaw = ang.dot(up);
      const a = Math.abs(com.clone().sub(pos).applyQuaternion(q.clone().invert()).z - this.anchorFrontZ);
      const neutral = Math.atan2(vLeft + a * yaw, this.forwardSpeed);
      const room = Math.atan(optAngle) * 1.1;
      delta = clamp(delta, neutral - room, neutral + room);
    }

    // ---- marcha, rpm y par del motor ----
    if (this.gear < 0 && input.throttle > 0 && this.forwardSpeed > -1.5) this.gear = 1;
    if (this.gear > 0 && input.brake > 0 && this.forwardSpeed < 1.2 && input.throttle === 0) this.gear = -1;
    // en marcha atras los pedales se invierten: S acelera hacia atras y W frena
    const reversing = this.gear < 0;
    const gas = reversing ? input.brake : input.throttle;
    const brakePedal = reversing ? (this.forwardSpeed < -1 ? input.throttle : 0) : input.brake;
    const ratio = this.gear > 0 ? this.ratios[this.gear] * this.finalDrive : this.reverseRatio * this.finalDrive;
    const rpmWheel = Math.max(0, this._drivenOmega() * ratio) / RPM_TO_RAD;
    this.shiftTimer = Math.max(0, this.shiftTimer - dt);
    this.shiftHold = Math.max(0, this.shiftHold - dt);
    if (this.gear > 0 && this.shiftTimer === 0 && this.shiftHold === 0) {
      // las decisiones de cambio usan el regimen que corresponde a la velocidad real del coche,
      // no el de las ruedas (que pueden patinar)
      const speedRpm = Math.abs(this.forwardSpeed) / R * this.ratios[this.gear] * this.finalDrive / RPM_TO_RAD;
      const up_ = Math.min(this.red * 0.97, this.rpmPower * 1.03), down = this.rpmTorque * 0.62;
      if (this.rpm > up_ && speedRpm > up_ * 0.8 && this.gear < this.numGears) {
        this.gear++; this.shiftTimer = 0.22; this.shiftHold = 0.6;
      } else if (this.gear > 1 && speedRpm < down &&
                 speedRpm * this.ratios[this.gear - 1] / this.ratios[this.gear] < this.red * 0.9) {
        this.gear--; this.shiftTimer = 0.15; this.shiftHold = 0.4;
      }
    }
    const launch = this.rpmTorque * 0.9;   // el embrague patina al arrancar: el motor no cae por debajo de este regimen
    const rpmTarget = Math.max(rpmWheel, rpmWheel < launch ? this.idle + (launch - this.idle) * gas : this.idle, this.idle);
    this.rpm += (rpmTarget - this.rpm) * Math.min(1, dt / 0.04);
    // nitro: mientras haya deposito y se acelere hacia delante, el par sube; se recarga poco a poco al soltarlo
    this.nitroActive = !!input.nitro && this.nitro > 0 && gas > 0 && this.gear > 0;
    if (this.nitroActive) this.nitro = Math.max(0, this.nitro - dt);
    else this.nitro = Math.min(this.nitroMax, this.nitro + dt * 0.08 * this.nitroMax);
    let Te = curve(this.torque, this.rpm) * gas * (this.nitroActive ? 1 + this.nitroBoost : 1);
    if (gas === 0) {                                                                        // freno motor
      const engaged = clamp((rpmWheel - this.idle * 0.8) / (this.idle * 0.6), 0, 1);       // solo si las ruedas giran
      Te = -(0.06 + 0.2 * this.rpm / this.red) * this.tPeak * engaged;
    }
    if (this.rpm > this.red * 1.02) Te = Math.min(Te, 0);                                   // limitador
    Te = clamp(Te, -this.clutchTorque, this.clutchTorque);
    const nDriven = (this.front ? 2 : 0) + (this.rear ? 2 : 0);
    const wheelTorque = this.shiftTimer > 0 ? 0 : Te * ratio * this.drivetrainEff / nDriven;
    // anti-patinaje (AntiSpinMultiplier del juego): recorta el par de la rueda que patina de mas
    const antiSpin = this.antiSpin;

    // ---- suspension: abanico de rayos a lo largo del perfil de la rueda ----
    // Un circulo de radio R que rueda sobre el terreno apoya su centro a la altura
    // max_s( suelo(s) + sqrt(R^2 - s^2) ). Se muestrea s con rayos verticales (exactos): sube
    // bordillos, escalones y pendientes de forma continua. (El barrido de formas de Rapier se
    // probo y tiene errores de hasta 3 cm segun la posicion, inaceptables para un amortiguador.)
    for (const w of this.wheels) {
      w.steer = w.front ? delta : 0;
      const hp = w.anchor.clone().add(new THREE.Vector3(0, w.rest, 0)).applyQuaternion(q).add(pos);   // anclaje en mundo
      const down = { x: -up.x, y: -up.y, z: -up.z };
      const wf = fwd.clone().applyAxisAngle(up, w.steer);                                           // avance de la rueda
      w.hp = hp;
      w.contact = false;
      w.penetration = 0;
      w.L = w.maxL;
      let best = null;
      for (const s of this.profile) {
        const o = s === 0 ? hp : hp.clone().addScaledVector(wf, s);
        const h = this.world.castRayAndGetNormal(new RAPIER.Ray(o, down), w.maxL + R, true, undefined, undefined, undefined, rb);
        if (!h) continue;
        const n = new THREE.Vector3(h.normal.x, h.normal.y, h.normal.z);
        if (n.dot(up) < 0) n.negate();
        if (n.dot(up) < 0.35) continue;                                  // superficie casi vertical: no es suelo
        const L = h.timeOfImpact - Math.sqrt(R * R - s * s);
        if (!best || L < best.L) best = { L, h, n, s, o };
      }
      if (best) {
        const { L, h, n, s, o } = best;
        w.contact = true;
        w.L = clamp(L, w.minL, w.maxL);
        w.penetration = Math.max(0, w.minL - L);                         // el neumatico toca antes del tope: tope de goma
        w.point = o.clone().addScaledVector(up, -h.timeOfImpact);
        // normal de contacto: sobre suelo llano la del terreno; en un borde, la que une el punto
        // de contacto con el centro de la rueda (el neumatico es redondo)
        if (Math.abs(s) < 1e-6) w.normal = n;
        else w.normal = hp.clone().addScaledVector(up, -L).sub(w.point).normalize();
        w.surf = this.surfaceAt ? this.surfaceAt(h) : null;
        w.dyn = (w.surf && w.surf.dyn) || this.defaultDyn;
      }
      const rate_ = (w.prevL - w.L) / dt;                                           // > 0: comprimiendo
      const F = w.contact ? w.k * (w.rest - w.L) + (rate_ > 0 ? w.cBump : w.cReb) * rate_ + w.k * 8 * w.penetration : 0;
      w.Fspring = Math.max(0, F);
      w.prevL = w.L;
    }
    // ayuda de estabilidad que impone el suelo (SlideControl de su dinamica), media de las ruedas apoyadas
    {
      let sum = 0, n = 0;
      for (const w of this.wheels) if (w.contact && w.dyn) { sum += w.dyn.SlideControl || 0; n++; }
      this._surfAssist = n ? sum / n * tune.surface : 0;
    }
    // barra estabilizadora: transfiere carga entre las ruedas del mismo eje
    for (const [ia, ib] of [[0, 1], [2, 3]]) {
      const a = this.wheels[ia], b = this.wheels[ib];
      if (a.contact && b.contact) {
        const dF = a.rollbar * a.k * ((a.rest - a.L) - (b.rest - b.L));
        a.Fspring = Math.max(0, a.Fspring + dF); b.Fspring = Math.max(0, b.Fspring - dF);
      }
    }

    // ---- neumaticos: un paso por rueda ----
    const impulse = (F, p) => rb.applyImpulseAtPoint({ x: F.x * dt, y: F.y * dt, z: F.z * dt }, p, true);
    for (const w of this.wheels) {
      const driven = w.front ? this.front : this.rear;
      // dinamica de neumatico del suelo bajo esta rueda; el ajuste "surface" mezcla entre la del
      // suelo real y la de referencia (asfalto): 0 = todos los suelos como asfalto
      const ref = this.defaultDyn, dyn = w.dyn || ref;
      const mix = (a, b) => b + (a - b) * tune.surface;
      const dMuX = mix(dyn.XFriction[0], ref.XFriction[0]), dMuZ = mix(dyn.ZFriction[0], ref.ZFriction[0]);
      const xStiff = clamp(mix(dyn.XStiffness[0], ref.XStiffness[0]), 0.35, 1.3);
      const zStiff = clamp(mix(dyn.ZStiffness[0], ref.ZStiffness[0]), 0.35, 1.3);
      const boost = mix(dyn.FrictionBoost, ref.FrictionBoost), slowDown = mix(dyn.SlowDown, ref.SlowDown);
      const wAngle = optAngle / xStiff, wRatio = this.optSlipRatio / zStiff;
      let Td = driven ? wheelTorque : 0;
      const as = antiSpin * mix(dyn.AntiSpin, ref.AntiSpin);
      if (Td > 0 && as > 0) {
        // el rozamiento disponible se reparte entre empuje y agarre lateral: si la rueda ya patina
        // (longitudinal) o derrapa (lateral), se recorta el par para dejar agarre lateral
        const exLong = Math.abs(w.slipRatio) / wRatio - 0.5;
        const exLat = Math.abs(w.syFiltered) - 0.45;
        Td *= clamp(1 - as * Math.max(0, exLong) / 0.6, 0.1, 1) * clamp(1 - as * Math.max(0, exLat) / 0.8, 0.1, 1);
      }
      const axleBrake = (w.front ? this.brakeBalance : 1 - this.brakeBalance) * this.brakeTorque / 2;
      const hold = gas === 0 && brakePedal === 0 && Math.abs(this.forwardSpeed) < 0.4 ? 400 : 0;   // retencion: no se mueve parado
      const Tb = axleBrake * brakePedal + hold +
                 (!w.front && input.handbrake ? this.handbrakeTorque / 2 : 0);

      if (!w.contact) {   // en el aire la rueda gira libre
        w.omega += dt * (Td - Tb * Math.tanh(w.omega / 0.3)) / w.inertia;
        w.Fz = 0; w.slipRatio = w.slipAngle = 0; w.spin += w.omega * dt;
        continue;
      }
      // ejes del neumatico sobre el suelo
      const n = w.normal;
      const f0 = fwd.clone().applyAxisAngle(up, w.steer);
      const f = f0.addScaledVector(n, -f0.dot(n)).normalize();
      const rgt = new THREE.Vector3().crossVectors(f, n).normalize();
      const rel = w.point.clone().sub(com);
      const vc = lin.clone().add(new THREE.Vector3().crossVectors(ang, rel));
      const vx = vc.dot(f), vy = vc.dot(rgt), speed = Math.hypot(vx, vy);
      const Fz = w.Fspring; w.Fz = Fz;
      const muX = this.muX * tune.grip * dMuX * (!w.front && input.handbrake ? 0.7 : 1), muZ = this.muZ * tune.grip * dMuZ;
      const mCorner = this.mass / 4;

      // deslizamiento lateral filtrado (relajacion): se acerca a su valor instantaneo segun lo recorrido
      const syRaw = (vy / Math.max(Math.abs(vx), 1.5)) / wAngle;
      w.syFiltered += (syRaw - w.syFiltered) * Math.min(1, speed * dt / tune.relax);
      const syUsed = w.syFiltered;
      const forces = om => {
        const kappa = (om * R - vx) / Math.max(Math.abs(vx), 1.0);
        const sx = kappa / wRatio;
        const sy = syUsed;
        const s = Math.hypot(sx, sy) || 1e-6;
        // pico en s ~ 0.8 y ~70% al deslizar; FrictionBoost del suelo sube ese rozamiento de deslizamiento
        const g = Math.sin(1.5 * Math.atan(2.2 * s)) / 0.989 + boost * clamp((s - 0.8) / 1.0, 0, 1);
        const Fx = Fz * muZ * g * sx / s;
        let Fy = -Fz * muX * g * sy / s;
        // a muy baja velocidad el rozamiento lateral "agarra" en vez de deslizar
        const blend = clamp((speed - 0.5) / 2.5, 0, 1);
        const stick = clamp(-mCorner * vy / dt * 0.5, -muX * Fz, muX * Fz);
        Fy = stick * (1 - blend) + Fy * blend;
        return [Fx, Fy, kappa, Math.atan(vy / Math.max(Math.abs(vx), 1.5))];
      };
      // la rueda se resuelve de forma implicita para no oscilar con la rigidez del neumatico.
      // Newton amortiguado: la pendiente se fuerza a ser positiva (mas alla del pico de
      // friccion la curva baja y un Newton puro divergeria).
      let om = w.omega;
      const resid = o => w.inertia * (o - w.omega) / dt - (Td - Tb * Math.tanh(o / 0.3)) + R * forces(o)[0];
      for (let it = 0; it < 5; it++) {
        const f0r = resid(om), slope = (resid(om + 0.05) - f0r) / 0.05;
        const d = Math.max(slope, w.inertia / dt);
        om -= clamp(f0r / d, -30, 30);
      }
      w.omega = om;
      w.spin += om * dt;
      const [Fx, Fy, kappa, alpha] = forces(om);
      w.slipRatio = kappa; w.slipAngle = alpha;
      // resistencia a la rodadura del suelo (por coche: RollingResistance de sus neumaticos) y freno extra
      // de la superficie (SlowDown: arena, objetos...)
      const rolling = this.rollResCar * mix(dyn.RollingResistance, ref.RollingResistance) + slowDown * clamp(Math.abs(vx) / 4, 0, 1);
      const rr = -rolling * Fz * Math.sign(vx) * clamp(Math.abs(vx) / 0.5, 0, 1);

      impulse(f.clone().multiplyScalar(Fx + rr).addScaledVector(rgt, Fy), w.point);
      // el muelle empuja segun la normal del suelo (con el chasis inclinado, empujar segun su eje
      // dejaria una componente horizontal y el coche parado se iria rodando)
      impulse(n.clone().multiplyScalar(w.Fspring), w.hp.clone().addScaledVector(up, -w.L));
    }

    // ---- ayuda de estabilidad (SlideControl): lleva la guiñada hacia la que pide el volante ----
    if (assist > 0 && this.forwardSpeed > 8) {
      // solo corrige el exceso de rotacion (sobreviraje o trompo); nunca empuja a girar mas de lo que
      // el volante pide, para no forzar al coche mas alla del agarre
      const yawRate = ang.dot(up);
      const wanted = this.forwardSpeed * Math.tan(delta) / this.wheelbase;
      const sameSide = yawRate * wanted > 0;
      const excess = sameSide
        ? (Math.abs(yawRate) > Math.abs(wanted) ? yawRate - wanted : 0)       // gira mas de lo pedido
        : yawRate;                                                            // gira al reves de lo pedido
      if (Math.abs(excess) > 0.15) {
        const tq = clamp(-assist * this.yawInertia * 1.8 * (excess - Math.sign(excess) * 0.15),
          -this.yawInertia * 5, this.yawInertia * 5);
        rb.applyTorqueImpulse({ x: up.x * tq * dt, y: up.y * tq * dt, z: up.z * tq * dt }, true);
      }
    }

    // ---- aerodinamica ----
    const v2 = lin.lengthSq(), sp = Math.sqrt(v2);
    if (sp > 0.1) {
      impulse(lin.clone().multiplyScalar(-this.aeroDrag * sp), pos.clone().add(this.aeroDragLoc.clone().applyQuaternion(q)));
      impulse(up.clone().multiplyScalar(-this.downforce * 0.25 * v2), pos);
    }
  }

  _drivenOmega() {
    let sum = 0, n = 0;
    for (const w of this.wheels) if (w.front ? this.front : this.rear) { sum += w.omega; n++; }
    return n ? sum / n : 0;
  }
}
