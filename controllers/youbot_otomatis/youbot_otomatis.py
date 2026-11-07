"""
youbot_pindah_barang.py - KUKA youBot (Webots): ambil, maju/mundur, putar, taruh barang.

Dasarnya kodemu yang sudah berhasil mencapit dan memindahkan barang.
GERAKAN TIDAK DIUBAH. Yang ditambahkan hanya:
  1. Kamera safety: kotak HIJAU terang di pandangan kamera = "orang" -> robot berhenti
     sampai orang pergi, lalu lanjut.
  2. Log CSV: log_robot.csv (detail) dan ringkasan_siklus.csv (satu baris per run).
  3. Verifikasi capit: setelah ambil(), dicetak apakah capit memegang barang.
"""
import csv
import math
import os
import time

from controller import Robot, Node

# ======================= PENGATURAN (silakan diubah) =======================
KECEPATAN_MAKS = 6.0      # rad/s roda (batas youBot 14.8)
KECEPATAN_PUTAR = 3.0     # rad/s roda saat berputar
LEBAR_BARANG = 0.04       # m, lebar kardus
BUKA_CAPIT = 0.024        # m, bukaan maksimum tiap jari youBot
KECEPATAN_LENGAN = 0.6    # rad/s

POSE_AMAN = (0.0, 1.57, -2.635, 1.78, 0.0)      # lengan terlipat
POSE_AMBIL = (0.0, -0.27, -1.67, -1.20, 0.0)    # menjangkau ke depan, rendah
POSE_TARUH = (0.0, -0.62, -0.98, -1.53, 0.0)    # tinggi conveyor
ANGKAT_ARM2 = 0.4         # tambahan arm2 untuk mengangkat barang

JARAK_DEPAN_KE_TEPI_MEJA = 0.43
JARAK_BERHENTI_DARI_TEPI = 0.05
MAJU_KE_MEJA = 0.36

JARAK_DEPAN_KE_TEPI_KONVEYOR = 0.19
JARAK_BERHENTI_DARI_KONVEYOR = 0.04
MAJU_KE_KONVEYOR = JARAK_DEPAN_KE_TEPI_KONVEYOR - JARAK_BERHENTI_DARI_KONVEYOR

# Urutan tugas. Jarak dalam meter, sudut dalam derajat
MISI = [
    ("maju", MAJU_KE_MEJA),
    ("ambil",),
    ("mundur", MAJU_KE_MEJA),
    ("putar", 90),
    ("maju_sampai_y", 0.16),
    ("taruh",),
]


# --- tambahan: safety kamera & log ---
NAMA_KAMERA = "CCTV_PABRIK"
CEK_KAMERA_SETIAP = 4       # tiap n langkah simulasi
LANGKAH_PIKSEL = 8          # ambil sampel tiap n piksel gambar
AMBANG_ORANG = 0.015        # rasio piksel hijau minimal untuk dianggap orang
AMAN_BERTURUT = 10          # cek berturut-turut tanpa orang sebelum lanjut
LOG_SETIAP = 5              # tiap n langkah simulasi
NAMA_LOG = "log_robot.csv"
NAMA_RINGKASAN = "ringkasan_siklus.csv"
# ===========================================================================
GRIP_MIN_TERISI = 0.0085
robot = Robot()
TS = int(robot.getBasicTimeStep())


# ---------- cari perangkat ----------
def cari_tipe(tipe):
    for i in range(robot.getNumberOfDevices()):
        d = robot.getDeviceByIndex(i)
        if d.getNodeType() == tipe:
            return d
    return None


gps = cari_tipe(Node.GPS)
if gps:
    gps.enable(TS)
imu = cari_tipe(Node.INERTIAL_UNIT)
if imu:
    imu.enable(TS)

# Kamera CCTV: harus di-enable, kalau tidak gambarnya hitam
cam = robot.getDevice(NAMA_KAMERA)
if cam:
    cam.enable(TS * CEK_KAMERA_SETIAP)

roda = [robot.getDevice("wheel%d" % i) for i in range(1, 5)]
for r in roda:
    r.setPosition(float("inf"))
    r.setVelocity(0.0)

lengan = [robot.getDevice("arm%d" % i) for i in range(1, 6)]
for m in lengan:
    m.setVelocity(KECEPATAN_LENGAN)
    s = m.getPositionSensor()
    if s:
        s.enable(TS)

jari = [robot.getDevice("finger::left"), robot.getDevice("finger::right")]
for j in jari:
    j.setVelocity(0.03)
    j.setAvailableForce(10.0)    # gaya jepit tiap jari (N)
    s = j.getPositionSensor()
    if s:
        s.enable(TS)

# ---------- status untuk safety & log ----------
STATE = "INIT"
n_langkah = 0
orang = False
rasio_orang = 0.0
n_jeda = 0
total_jeda = 0.0
target_lengan = None
HASIL = {"ambil_ok": None}

f_log = open(NAMA_LOG, "w", newline="")
log = csv.writer(f_log)
log.writerow(["t_s", "state", "gps_x", "gps_y", "yaw_deg",
              "capit", "rasio_orang", "orang", "event"])


# ---------- fungsi dasar (didefinisikan sebelum langkah pertama) ----------
def set_roda(a, b, c, d):
    for r, v in zip(roda, (a, b, c, d)):
        r.setVelocity(v)


def posisi():
    return gps.getValues() if gps else None


def lebar_capit():
    nilai = [j.getPositionSensor().getValue() for j in jari]
    return sum(nilai) / len(nilai)


def catat(event=""):
    p = posisi()
    x, y = (p[0], p[1]) if p else (0.0, 0.0)
    yaw = math.degrees(imu.getRollPitchYaw()[2]) if imu else 0.0
    log.writerow(["%.3f" % robot.getTime(), STATE, "%.4f" % x, "%.4f" % y,
                  "%.2f" % yaw, "%.4f" % lebar_capit(), "%.4f" % rasio_orang,
                  int(orang), event])
    f_log.flush()


def orang_terlihat():
    """Orang = piksel hijau terang di kamera. Kardus cokelat diabaikan."""
    global orang, rasio_orang
    gambar = cam.getImage()
    if gambar is None:
        return False
    w, h = cam.getWidth(), cam.getHeight()
    hijau = 0
    total = 0
    for y in range(0, h, LANGKAH_PIKSEL):
        baris = y * w * 4
        for x in range(0, w, LANGKAH_PIKSEL):
            i = baris + x * 4
            b, g, r = gambar[i], gambar[i + 1], gambar[i + 2]
            total += 1
            if g > 110 and g > 1.5 * r and g > 1.5 * b:
                hijau += 1
    rasio_orang = hijau / total if total else 0.0
    orang = rasio_orang >= AMBANG_ORANG
    return orang


def jeda_safety():
    """Berhenti total sampai area aman beberapa cek berturut-turut."""
    global STATE, n_jeda, total_jeda, n_langkah
    state_lama = STATE
    STATE = "BERHENTI_SAFETY"
    n_jeda += 1
    print("[%7.2fs] ORANG TERDETEKSI (rasio %.3f) -> STOP"
          % (robot.getTime(), rasio_orang))
    catat("SAFETY_STOP")
    mulai = robot.getTime()
    pos = [m.getPositionSensor().getValue() for m in lengan]
    aman = 0
    while aman < AMAN_BERTURUT:
        set_roda(0, 0, 0, 0)
        for m, p in zip(lengan, pos):
            m.setPosition(p)          # tahan lengan di posisi sekarang
        if robot.step(TS) == -1:
            raise SystemExit
        n_langkah += 1
        if n_langkah % CEK_KAMERA_SETIAP == 0:
            aman = 0 if orang_terlihat() else aman + 1
        if n_langkah % LOG_SETIAP == 0:
            catat()
    total_jeda += robot.getTime() - mulai
    if target_lengan is not None:     # lanjutkan gerak lengan yang tertunda
        for m, p in zip(lengan, target_lengan):
            m.setPosition(p)
    STATE = state_lama
    print("[%7.2fs] AREA AMAN -> LANJUT" % robot.getTime())
    catat("SAFETY_LANJUT")


def langkah():
    global n_langkah
    if robot.step(TS) == -1:
        raise SystemExit
    n_langkah += 1
    if cam and n_langkah % CEK_KAMERA_SETIAP == 0:
        if orang_terlihat():
            jeda_safety()
    if n_langkah % LOG_SETIAP == 0:
        catat()


def tunggu(detik):
    for _ in range(int(detik * 1000 / TS)):
        langkah()


langkah()  # satu langkah agar sensor terisi


# ---------- gerak roda ----------
def berhenti():
    set_roda(0, 0, 0, 0)
    tunggu(0.3)


def jarak(p, q):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(p, q)))


def jalan(arah, target, batas_detik=15):
    """arah: (a,b,c,d) pengali kecepatan roda. Berhenti di jarak 'target' (m)."""
    if gps:
        awal = list(posisi())
        t0 = robot.getTime()
        while True:
            if robot.getTime() - t0 > batas_detik:
                print("jalan: batas waktu, robot mungkin tertahan")
                break
            langkah()
            sisa = target - jarak(awal, posisi())
            if sisa <= 0.003:
                break
            v = min(KECEPATAN_MAKS, max(1.0, sisa * 12))  # melambat dekat target
            set_roda(*[v * k for k in arah])
    else:
        # tanpa GPS: perkiraan waktu (youBot ~0.05 m per rad/s roda)
        v = KECEPATAN_MAKS
        set_roda(*[v * k for k in arah])
        tunggu(target / (0.05 * v))
    berhenti()


def maju(m):
    jalan((1, 1, 1, 1), m)


def mundur(m):
    jalan((-1, -1, -1, -1), m)


def geser_kiri(m):
    jalan((-1, 1, 1, -1), m)


def geser_kanan(m):
    jalan((1, -1, -1, 1), m)


def putar(derajat):
    """Positif = putar kiri. Memakai InertialUnit bila ada, kalau tidak pakai waktu."""
    arah = 1 if derajat > 0 else -1
    pola = (arah, -arah, arah, -arah)
    if imu:
        langkah()
        target = math.radians(abs(derajat))
        terakhir = imu.getRollPitchYaw()[2]
        total = 0.0
        n = 0
        while total < target - 0.05:
            langkah()
            yaw = imu.getRollPitchYaw()[2]
            d = yaw - terakhir
            d = (d + math.pi) % (2 * math.pi) - math.pi  # normalisasi
            total += abs(d)
            terakhir = yaw
            v = min(KECEPATAN_PUTAR, max(2.0, (target - total) * 4))
            set_roda(*[v * k for k in pola])
            n += 1
            if n % 20 == 0:
                print("putar: yaw=%.2f total=%.2f v=%.2f gps=(%.2f, %.2f)"
                      % (yaw, total, v, posisi()[0], posisi()[1]))
    else:
        # perkiraan kasar, kalibrasi DETIK_PER_DERAJAT sendiri
        DETIK_PER_DERAJAT = 0.0175
        set_roda(*[KECEPATAN_PUTAR * k for k in pola])
        tunggu(abs(derajat) * DETIK_PER_DERAJAT)
    berhenti()


def maju_sampai_y(y_target, batas_detik=15):
    """Maju (robot menghadap +y) sampai GPS y mencapai y_target."""
    t0 = robot.getTime()
    while True:
        langkah()
        if robot.getTime() - t0 > batas_detik:
            print("maju_sampai_y: batas waktu")
            break
        sisa = y_target - posisi()[1]
        if sisa <= 0.003:
            break
        v = min(KECEPATAN_MAKS, max(1.0, sisa * 10))
        set_roda(v, v, v, v)
    berhenti()


# ---------- lengan & capit ----------
def pose(nilai, timeout=6.0):
    global target_lengan
    target_lengan = list(nilai)
    for m, p in zip(lengan, nilai):
        m.setPosition(p)
    for _ in range(int(timeout * 1000 / TS)):
        langkah()
        galat = 0.0
        for m, p in zip(lengan, nilai):
            s = m.getPositionSensor()
            if s:
                galat = max(galat, abs(s.getValue() - p))
        if galat < 0.02:
            break
    tunggu(0.2)


def capit(bukaan):
    for j in jari:
        j.setPosition(bukaan)
    tunggu(1.0)


def tutup_capit():
    # target lebih sempit dari setengah lebar kardus supaya jari menekan
    capit(max(0.0, LEBAR_BARANG / 2 - 0.012))   # = 0.008
    nilai = [j.getPositionSensor().getValue() for j in jari]
    print("Posisi jari setelah menutup:", nilai)


def angkat(nilai):
    n = list(nilai)
    n[1] += ANGKAT_ARM2
    return tuple(n)


def ambil():
    capit(BUKA_CAPIT)
    pose(angkat(POSE_AMBIL))   # arahkan ke depan dulu, di atas barang
    pose(POSE_AMBIL )          # turun            
    tutup_capit()              # capit barang
    pose(angkat(POSE_AMBIL))   # angkat
    # verifikasi (tidak mengubah gerakan)
    rata = lebar_capit()
    HASIL["ambil_ok"] = rata > GRIP_MIN_TERISI
    teks = "MEMEGANG_BARANG" if HASIL["ambil_ok"] else "KOSONG (barang tidak terjepit)"
    print("AMBIL: capit=%.4f -> %s" % (rata, teks))
    catat("AMBIL_" + ("OK" if HASIL["ambil_ok"] else "GAGAL"))

def taruh():
    pose(angkat(POSE_TARUH))
    capit(BUKA_CAPIT)          # lepas barang
    pose(angkat(POSE_TARUH))
    pose(POSE_AMAN, 8.0)       # lipat lengan setelah kardus lepas


# ---------- jalankan misi ----------
def jalankan(perintah):
    global STATE
    nama = perintah[0]
    arg = perintah[1:]
    STATE = nama.upper()
    p = posisi()
    print("[%s %s] GPS awal: %s" % (nama, arg, p))
    catat("MULAI_" + nama)
    fungsi = {
        "maju": maju, "mundur": mundur,
        "geser_kiri": geser_kiri, "geser_kanan": geser_kanan,
        "putar": putar, "ambil": ambil, "taruh": taruh,
        "maju_sampai_y": maju_sampai_y,
    }[nama]
    fungsi(*arg)
    print("   GPS akhir: %s" % (posisi(),))


def tulis_ringkasan(durasi):
    baru = (not os.path.exists(NAMA_RINGKASAN)
            or os.path.getsize(NAMA_RINGKASAN) == 0)
    hasil = "SUKSES" if HASIL["ambil_ok"] else "GAGAL_AMBIL"
    with open(NAMA_RINGKASAN, "a", newline="") as f:
        w = csv.writer(f)
        if baru:
            w.writerow(["waktu_selesai", "hasil", "durasi_s", "stop_safety",
                        "total_jeda_s", "capit_terisi"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), hasil, "%.1f" % durasi,
                    n_jeda, "%.1f" % total_jeda, HASIL["ambil_ok"]])


try:
    t_mulai = robot.getTime()
    capit(BUKA_CAPIT)
    pose(POSE_AMAN)
    tunggu(0.5)

    for perintah in MISI:
        jalankan(perintah)

    STATE = "SELESAI"
    print("Misi selesai.")
    catat("SELESAI")
    tulis_ringkasan(robot.getTime() - t_mulai)
    while True:
        langkah()
finally:
    f_log.flush()
    f_log.close()
