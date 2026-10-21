# Week 1 research notes: sensors, frames, timing

These notes cover Day 1 (sensor and API survey), the Week 1 research questions, and addendum section A (frames
and inertial sensing, including the required exercises).

## 1. Phone sensors and APIs (Day 1)

| Sensor | Android | iOS | Browser (what we use) | Typical rate | Units, notes |
|---|---|---|---|---|---|
| Camera | Camera2 / CameraX; `SENSOR_TIMESTAMP` | AVFoundation; sample-buffer presentation time | `getUserMedia` + `requestVideoFrameCallback` (`captureTime`) | 30 (60) fps | Browser frames are already rotated to the screen orientation |
| Gyroscope | `TYPE_GYROSCOPE` (+ `_UNCALIBRATED`) | `CMGyroData`, `CMDeviceMotion.rotationRate` | `Gyroscope` (Generic Sensor), or `devicemotion.rotationRate` | Android 200–500 Hz (200 Hz cap since Android 12 without `HIGH_SAMPLING_RATE_SENSORS`); iOS ≤100 Hz; browser ~60 Hz | rad/s. `devicemotion` reports **deg/s**, so the client converts. |
| Accelerometer | `TYPE_ACCELEROMETER` (incl. gravity), `TYPE_LINEAR_ACCELERATION` (fused, gravity removed) | `CMAccelerometerData` in **g**, `userAcceleration` + `gravity` | `Accelerometer`, or `devicemotion.accelerationIncludingGravity` | As gyro | m/s². Lying face-up, Android/W3C read z ≈ **+9.81**. iOS Core Motion reads z ≈ **−1 g**. Verify the sign with the flat test (stress test). |
| Magnetometer | `TYPE_MAGNETIC_FIELD` | `CMMagnetometerData` | `Magnetometer` (flagged) | 50–100 Hz | µT. Unreliable inside a car (steel, motors). |
| Fused orientation | `TYPE_ROTATION_VECTOR`, `TYPE_GAME_ROTATION_VECTOR` | `CMDeviceMotion.attitude` | `AbsoluteOrientationSensor`, `deviceorientation` | ~60–200 Hz | The OS's own filter; not used (we want raw data) |
| GNSS | `FusedLocationProvider`, raw `GnssMeasurement` (Android 7+) | Core Location | `geolocation.watchPosition` | 1 Hz (up to 10 Hz on some phones) | Fix time is UTC wall clock. Accuracy ≈ 3–10 m. |
| Barometer | `TYPE_PRESSURE` | `CMAltimeter` | none | 10–25 Hz | hPa; relative altitude |

**Body axes.** Android, iOS and W3C share the same device frame: x to the right of the screen, y toward the top of
the screen, z out of the screen (right-handed). The screen auto-rotating does not change sensor axes, but it does
rotate the browser's video frames. That is why `orientation_deg` is logged with every frame.

**Permissions and privacy.** Camera access always needs explicit user consent. On iOS, motion needs
`DeviceMotionEvent.requestPermission()` from a tap. Location needs consent. Browsers expose camera and sensors
only in a **secure context** (`https://` or `localhost`). Browsers also coarsen `performance.now()` to 0.1–1 ms
unless the page is cross-origin isolated; our server sends COOP/COEP headers to get 5–20 µs. Finally, browsers
cap motion-sensor rates (commonly ~60 Hz) to limit fingerprinting.

**Clocks.**
* Android: sensor and camera timestamps use `elapsedRealtimeNanos` when `SENSOR_INFO_TIMESTAMP_SOURCE = REALTIME`;
  with `UNKNOWN`, the camera clock is not comparable to the sensor clock.
* iOS: host time (`mach_absolute_time`) for both.
* Browser: `performance.now()` for frames, sensor readings and events.

## 2. Research questions

### Q1. What do the gyroscope and accelerometer measure, and why is raw accelerometer data not vehicle acceleration?

* **Gyroscope (MEMS).** A vibrating proof mass experiences a Coriolis force proportional to rotation. The output
  is the body's angular velocity relative to inertial space, expressed in **phone axes**:
  `ω_meas = ω + b_g + n_g` (plus scale-factor and axis-misalignment errors that vary with temperature). The
  Earth's rotation (7.3·10⁻⁵ rad/s) is far below the noise floor of a phone gyroscope.
* **Accelerometer (MEMS).** A proof mass on a spring measures **specific force**: `f_B = R_BW (a_W − g_W) + b_a + n_a`.
  With `g_W = (0, 0, −9.81)`, a phone at rest reads **+9.81 m/s² pointing up**, even though it is not
  accelerating.

Raw accelerometer data is not vehicle acceleration for six reasons:
1. **Gravity dominates.** It is 9.81 m/s², while normal driving produces 0.5–3 m/s². Removing gravity needs an
   accurate tilt: a 1° error leaks 0.17 m/s², about the size of gentle braking.
2. **Wrong axes.** Readings are in phone axes, not vehicle axes, so the mount rotation `R_VB` must be known.
3. **Lever arm.** The phone is not at the vehicle's centre of rotation. When yawing, it also measures
   `ω × (ω × r)` and `ω̇ × r`.
4. **Road grade.** On a slope, gravity projects onto the forward axis. Without extra information this is
   indistinguishable from acceleration.
5. **Vibration.** The engine, the road and holder resonance add 5–50 Hz content.
6. **Bias and noise.** Double integration turns a 0.05 m/s² bias into 2.5 m of position error after 10 s.

### Q2. How do we represent phone, camera and vehicle/world axes? What happens if the mount rotates?

Frames: phone body **B**, rear camera **C** (OpenCV: x right, y down, z = optical axis), vehicle **V** (ISO 8855:
x forward, y left, z up), world **W** (z up). A vector changes frame by rotation and a point by rotation plus
translation, e.g. `p_W = R_WB p_B + t_WB`.

```
        y_B (top of screen)                 rear camera, portrait (orientation 0):
        ^                                      x_C = +x_B   (image right)
        |   +-------+                          y_C = -y_B   (image down)
        |   | phone |                          z_C = -z_B   (looks out of the back)
        |   |screen |
        +---+-------+---> x_B               vehicle (camera looking ahead, level):
       /                                       x_V = z_C, y_V = -x_C, z_V = -y_C
     z_B (out of screen, toward the user)   gravity: g_W = (0,0,-9.81); at rest the accelerometer reads +g "up"
```

* `R_CB` is fixed by the hardware and by how the browser rotates frames. There is one matrix per screen
  orientation in [pc_app/sync.py](../../pc_app/sync.py), checked from data by `sync_check` (per-axis sign test).
* `R_VB` (the mount) must be calibrated. Roll and pitch come from gravity at standstill. Yaw comes from driving
  straight while accelerating (the horizontal specific force points forward), or from GNSS course against
  integrated gyro yaw.
* **If the mount rotates** (the holder slips, someone adjusts it, or the screen auto-rotates), every vehicle-frame
  quantity becomes wrong: "forward" acceleration picks up gravity, and lane/heading cues misalign. To detect it,
  watch for the gravity direction at stops changing and for `orientation_deg` changes, which are logged per frame.
  To mitigate it, lock screen rotation, use a rigid mount, and re-estimate `R_VB` at every stop.

### Q3. Why do rolling shutter, clock skew, transport buffering and unsynchronised streams damage fusion?

Fusion assumes that measurements combined in one update describe the **same instant** in the **same frame**.
Each effect breaks that:

* **Rolling shutter.** Rows are exposed one after another over roughly 10–30 ms of readout, so under rotation each
  row sees a different pose. Example: at 0.5 rad/s yaw with 20 ms of readout, the top and bottom rows differ by
  10 mrad, about 9 px at f = 900 px. Straight lines bend and boxes shear. A single frame timestamp is also
  ambiguous (start, middle or end of exposure), which shows up as a constant camera–IMU offset `td`.
* **Clock offset and skew.** Phone and PC oscillators differ by tens of ppm: 50 ppm × 10 min = 30 ms. Stamping
  frames with **PC receive time** instead adds 5–100 ms of variable Wi-Fi delay. We avoid both by stamping all
  phone data on the phone clock and modelling offset and drift separately ([pc_app/clock.py](../../pc_app/clock.py)).
* **Transport buffering.** Encoder, TCP and Wi-Fi queues add variable delay and burstiness (IMU arrives in 40 ms
  batches). Arrival order and time are not capture order and time. Using them as timestamps creates false
  accelerations and jitter.
* **Unsynchronised streams.** A 30 ms error at 0.5 rad/s gives 15 mrad, about 13 px, of wrongly predicted image
  motion. At 25 m/s it is 0.75 m of travel. A filter then sees innovations that its noise model cannot explain:
  it becomes over-confident, rejects good measurements, or diverges.

## 3. Addendum A exercises

**Numerical (required).** The measured yaw rate is 0.11 rad/s, of which 0.01 rad/s is bias, over 10 s:

| | Rate used | Yaw after 10 s |
|---|---|---|
| Without bias correction | 0.11 rad/s | 1.10 rad = **63.03°** |
| With bias correction | 0.10 rad/s | 1.00 rad = **57.30°** |
| Error | 0.01 rad/s | 0.10 rad = **5.73°**, growing linearly (34°/min) |

This is checked in `tests/test_core.py::test_yaw_bias_exercise`. On real data, estimate the bias from stationary
segments (the stress test's flat test). On the simulated session, bias correction removes 16.7° of false yaw
accumulated over 30 s.

**Code (required).** `python -m evaluation.imu_plots sessions/<rec_id>` plots:
* 3-axis gyro and accelerometer with stationary segments shaded;
* the naive yaw ∫ω_z against the heading change ∫ω·û, where û is "up" measured in the phone frame from gravity
  at rest.

For a phone lying flat, the two curves coincide. For an upright windshield mount, phone z is horizontal, so the
naive "yaw" is really pitch. That is the required case where phone orientation differs from car heading.

**Rotations.** Euler angles are intuitive but have singularities: at ±90° pitch, roll and yaw become the same
axis (gimbal lock), and composing Euler angles is awkward. Rotation matrices (9 numbers, 3 degrees of freedom)
compose by multiplication but drift off orthonormality when integrated. Unit quaternions (4 numbers, one
constraint) have no singularities, compose cheaply, renormalise trivially and interpolate smoothly (slerp),
which is why navigation filters use them. Our gyro integration composes small rotation vectors through the
exponential map ([pc_app/sync.py](../../pc_app/sync.py) `integrate_gyro`).
