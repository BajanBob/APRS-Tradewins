# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Satellite Watch Mode: ISS / APRS satellite pass prediction from your grid square, and the beacon schedule.

Orbit data comes from three places, and Tradewind uses the freshest and reports how well they agree:
  - N2YO (TLE, needs a free API key from n2yo.com),
  - AMSAT (nasabare.txt: the ISS and the amateur satellites),
  - NASA (the ISS's own trajectory file, J2000 state vectors - ISS only; also the fallback when no TLE is reachable),
  plus CelesTrak as a further TLE source.

Pure Python, no extra packages (so it also runs in the Android build):
  - SGP4 orbit propagation (near-earth model, WGS-72 constants), a port of the reference
    implementation by Vallado, Crawford, Hujsak and Kelso, "Revisiting Spacetrack Report #3" (2006);
  - look angles (azimuth / elevation / range) from your station;
  - passes: AOS, culmination (highest point) and LOS.

Beacon schedule during a pass (ISS digipeater, 145.825 MHz FM, path ARISS):
  - while the ISS is rising (before its highest point): one beacon a minute;
  - after the highest point, while it sets: at most 2 more beacons;
  - never more than 5 beacons in any 5 minutes.
Beacons go out only while the ISS is at or above the minimum elevation you set.
"""
import math
import time

ISS_CATNR = 25544
TLE_URL = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={ISS_CATNR}&FORMAT=TLE"
AMSAT_URL = "https://www.amsat.org/tle/current/nasabare.txt"
NASA_OEM_URL = "https://nasa-public-data.s3.amazonaws.com/iss-coords/current/ISS_OEM/ISS.OEM_J2K_EPH.txt"


def celestrak_url(catnr):
    return f"https://celestrak.org/NORAD/elements/gp.php?CATNR={int(catnr)}&FORMAT=TLE"


def n2yo_url(catnr, key):
    return f"https://api.n2yo.com/rest/v1/satellite/tle/{int(catnr)}&apiKey={key}"


# APRS satellites to choose from (all 1200 baud AFSK FM). Any other NORAD number can be entered.
SATELLITES = [
    {"catnr": 25544, "name": "ISS (ARISS digipeater)", "freq": "145.825", "path": "ARISS"},
]
ISS_FREQ_MHZ = "145.825"
ISS_PATH = "ARISS"

BEACON_GAP_S = 60          # one beacon a minute while rising
SETTING_MAX = 2            # at most this many after the highest point
WINDOW_S = 300             # ...and no more than WINDOW_MAX in any WINDOW_S seconds
WINDOW_MAX = 5

# WGS-72 constants (what the TLEs are fitted with)
_MU = 398600.8
_RE = 6378.135
_XKE = 60.0 / math.sqrt(_RE ** 3 / _MU)
_J2, _J3, _J4 = 0.001082616, -0.00000253881, -0.00000165597
_J3OJ2 = _J3 / _J2
_TWOPI = 2 * math.pi
_X2O3 = 2.0 / 3.0


# ---------------------------------------------------------------- TLE
def tle_checksum_ok(line):
    line = line.rstrip()
    if len(line) < 69 or not line[68].isdigit():
        return False
    s = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return s % 10 == int(line[68])


def parse_tle_text(text, catnr=None):
    """(name, line1, line2) from TLE text (2 or 3 line format; CelesTrak, AMSAT), or None.
    With catnr, only that satellite (AMSAT's file lists many)."""
    lines = [l.rstrip() for l in (text or "").splitlines() if l.strip()]
    for i in range(len(lines) - 1):
        l1, l2 = lines[i], lines[i + 1]
        if l1.startswith("1 ") and l2.startswith("2 ") and tle_checksum_ok(l1) and tle_checksum_ok(l2) \
                and l1[2:7].strip() == l2[2:7].strip() \
                and (catnr is None or l1[2:7].strip().lstrip("0") == str(int(catnr))):
            name = lines[i - 1].strip() if i > 0 and not lines[i - 1].startswith(("1 ", "2 ")) else "ISS (ZARYA)"
            return name, l1, l2
    return None


def parse_n2yo(data):
    """(name, line1, line2) from an N2YO /satellite/tle JSON reply, or None."""
    import json
    try:
        j = json.loads(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
        lines = [l.strip() for l in str(j.get("tle") or "").replace("\r", "").split("\n") if l.strip()]
        name = (j.get("info") or {}).get("satname") or "satellite"
    except (ValueError, AttributeError):
        return None
    got = parse_tle_text("\n".join(lines))
    return (name, got[1], got[2]) if got else None


def _tle_float(field):
    """'28098-4' / '-11606-4' / ' 00000+0' style assumed-decimal exponent field."""
    f = field.strip()
    if not f:
        return 0.0
    sign = -1.0 if f[0] == "-" else 1.0
    f = f.lstrip("+-")
    mant, exp = f[:-2], f[-2:]
    return sign * float("0." + mant) * 10 ** int(exp)


class Satellite:
    """SGP4 near-earth propagator for one TLE."""

    def __init__(self, line1, line2, name="ISS"):
        self.name = name
        self.catnr = int(line1[2:7])
        yy = int(line1[18:20])
        year = 2000 + yy if yy < 57 else 1900 + yy
        days = float(line1[20:32])
        self.epoch = _unix_of_year(year) + (days - 1.0) * 86400.0
        self.bstar = _tle_float(line1[53:61])
        self.inclo = math.radians(float(line2[8:16]))
        self.nodeo = math.radians(float(line2[17:25]))
        self.ecco = float("0." + line2[26:33].strip())
        self.argpo = math.radians(float(line2[34:42]))
        self.mo = math.radians(float(line2[43:51]))
        self.no_kozai = float(line2[52:63]) * _TWOPI / 1440.0     # rad / min
        self._init()

    def _init(self):
        ecco, inclo, argpo, mo, bstar = self.ecco, self.inclo, self.argpo, self.mo, self.bstar
        # ---- initl: recover the Brouwer mean motion
        eccsq = ecco * ecco
        omeosq = 1.0 - eccsq
        rteosq = math.sqrt(omeosq)
        cosio = math.cos(inclo)
        cosio2 = cosio * cosio
        ak = (_XKE / self.no_kozai) ** _X2O3
        d1 = 0.75 * _J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
        dl = d1 / (ak * ak)
        adel = ak * (1.0 - dl * dl - dl * (1.0 / 3.0 + 134.0 * dl * dl / 81.0))
        dl = d1 / (adel * adel)
        no = self.no_kozai / (1.0 + dl)
        ao = (_XKE / no) ** _X2O3
        sinio = math.sin(inclo)
        po = ao * omeosq
        con42 = 1.0 - 5.0 * cosio2
        con41 = -con42 - cosio2 - cosio2
        posq = po * po
        rp = ao * (1.0 - ecco)
        if _TWOPI / no >= 225.0:
            raise ValueError("deep-space orbit (period over 225 min) - not supported")
        self.no, self.ao, self.con41, self.cosio, self.sinio = no, ao, con41, cosio, sinio

        # ---- sgp4init (near earth)
        ss = 78.0 / _RE + 1.0
        qzms2t = ((120.0 - 78.0) / _RE) ** 4
        self.isimp = 1 if rp < (220.0 / _RE + 1.0) else 0
        sfour, qzms24 = ss, qzms2t
        perige = (rp - 1.0) * _RE
        if perige < 156.0:
            sfour = perige - 78.0
            if perige < 98.0:
                sfour = 20.0
            qzms24 = ((120.0 - sfour) / _RE) ** 4
            sfour = sfour / _RE + 1.0
        pinvsq = 1.0 / posq
        tsi = 1.0 / (ao - sfour)
        eta = ao * ecco * tsi
        etasq = eta * eta
        eeta = ecco * eta
        psisq = abs(1.0 - etasq)
        coef = qzms24 * tsi ** 4
        coef1 = coef / psisq ** 3.5
        cc2 = coef1 * no * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
                            + 0.375 * _J2 * tsi / psisq * con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
        cc1 = bstar * cc2
        cc3 = -2.0 * coef * tsi * _J3OJ2 * no * sinio / ecco if ecco > 1.0e-4 else 0.0
        x1mth2 = 1.0 - cosio2
        cc4 = 2.0 * no * coef1 * ao * omeosq * (
            eta * (2.0 + 0.5 * etasq) + ecco * (0.5 + 2.0 * etasq)
            - _J2 * tsi / (ao * psisq) * (-3.0 * con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                                          + 0.75 * x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq)) * math.cos(2.0 * argpo)))
        cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
        cosio4 = cosio2 * cosio2
        temp1 = 1.5 * _J2 * pinvsq * no
        temp2 = 0.5 * temp1 * _J2 * pinvsq
        temp3 = -0.46875 * _J4 * pinvsq * pinvsq * no
        self.mdot = no + 0.5 * temp1 * rteosq * con41 + 0.0625 * temp2 * rteosq * (13.0 - 78.0 * cosio2 + 137.0 * cosio4)
        self.argpdot = (-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                        + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
        xhdot1 = -temp1 * cosio
        self.nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio
        self.omgcof = bstar * cc3 * math.cos(argpo)
        self.xmcof = -_X2O3 * coef * bstar / eeta if ecco > 1.0e-4 else 0.0
        self.nodecf = 3.5 * omeosq * xhdot1 * cc1
        self.t2cof = 1.5 * cc1
        den = 1.0 + cosio if abs(cosio + 1.0) > 1.5e-12 else 1.5e-12
        self.xlcof = -0.25 * _J3OJ2 * sinio * (3.0 + 5.0 * cosio) / den
        self.aycof = -0.5 * _J3OJ2 * sinio
        self.delmo = (1.0 + eta * math.cos(mo)) ** 3
        self.sinmao = math.sin(mo)
        self.x7thm1 = 7.0 * cosio2 - 1.0
        self.x1mth2 = x1mth2
        self.cc1, self.cc4, self.cc5, self.eta = cc1, cc4, cc5, eta
        if not self.isimp:
            cc1sq = cc1 * cc1
            self.d2 = 4.0 * ao * tsi * cc1sq
            temp = self.d2 * tsi * cc1 / 3.0
            self.d3 = (17.0 * ao + sfour) * temp
            self.d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * cc1
            self.t3cof = self.d2 + 2.0 * cc1sq
            self.t4cof = 0.25 * (3.0 * self.d3 + cc1 * (12.0 * self.d2 + 10.0 * cc1sq))
            self.t5cof = 0.2 * (3.0 * self.d4 + 12.0 * cc1 * self.d3 + 6.0 * self.d2 * self.d2
                                + 15.0 * cc1sq * (2.0 * self.d2 + cc1sq))

    def propagate(self, tsince):
        """Position and velocity (km, km/s) in the TEME frame, tsince minutes after the TLE epoch."""
        t = tsince
        xmdf = self.mo + self.mdot * t
        argpdf = self.argpo + self.argpdot * t
        nodedf = self.nodeo + self.nodedot * t
        argpm, mm = argpdf, xmdf
        t2 = t * t
        nodem = nodedf + self.nodecf * t2
        tempa = 1.0 - self.cc1 * t
        tempe = self.bstar * self.cc4 * t
        templ = self.t2cof * t2
        if not self.isimp:
            delomg = self.omgcof * t
            delm = self.xmcof * ((1.0 + self.eta * math.cos(xmdf)) ** 3 - self.delmo)
            temp = delomg + delm
            mm = xmdf + temp
            argpm = argpdf - temp
            t3 = t2 * t
            t4 = t3 * t
            tempa = tempa - self.d2 * t2 - self.d3 * t3 - self.d4 * t4
            tempe = tempe + self.bstar * self.cc5 * (math.sin(mm) - self.sinmao)
            templ = templ + self.t3cof * t3 + t4 * (self.t4cof + t * self.t5cof)
        am = (_XKE / self.no) ** _X2O3 * tempa * tempa
        nm = _XKE / am ** 1.5
        em = self.ecco - tempe
        if em >= 1.0 or em < -0.001 or am < 0.95:
            raise ValueError("orbit has decayed or the TLE is too old")
        em = max(em, 1.0e-6)
        mm = mm + self.no * templ
        xlm = mm + argpm + nodem
        nodem %= _TWOPI
        argpm %= _TWOPI
        xlm %= _TWOPI
        mm = (xlm - argpm - nodem) % _TWOPI

        # long-period periodics
        axnl = em * math.cos(argpm)
        temp = 1.0 / (am * (1.0 - em * em))
        aynl = em * math.sin(argpm) + temp * self.aycof
        xl = mm + argpm + nodem + temp * self.xlcof * axnl
        # Kepler's equation
        u = (xl - nodem) % _TWOPI
        eo1, tem5, ktr = u, 9999.9, 1
        while abs(tem5) >= 1.0e-12 and ktr <= 10:
            sineo1, coseo1 = math.sin(eo1), math.cos(eo1)
            tem5 = 1.0 - coseo1 * axnl - sineo1 * aynl
            tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / tem5
            tem5 = max(-0.95, min(0.95, tem5))
            eo1 += tem5
            ktr += 1
        sineo1, coseo1 = math.sin(eo1), math.cos(eo1)
        # short-period preliminary quantities
        ecose = axnl * coseo1 + aynl * sineo1
        esine = axnl * sineo1 - aynl * coseo1
        el2 = axnl * axnl + aynl * aynl
        pl = am * (1.0 - el2)
        if pl < 0.0:
            raise ValueError("semi-latus rectum < 0")
        rl = am * (1.0 - ecose)
        rdotl = math.sqrt(am) * esine / rl
        rvdotl = math.sqrt(pl) / rl
        betal = math.sqrt(1.0 - el2)
        temp = esine / (1.0 + betal)
        sinu = am / rl * (sineo1 - aynl - axnl * temp)
        cosu = am / rl * (coseo1 - axnl + aynl * temp)
        su = math.atan2(sinu, cosu)
        sin2u = (cosu + cosu) * sinu
        cos2u = 1.0 - 2.0 * sinu * sinu
        temp = 1.0 / pl
        temp1 = 0.5 * _J2 * temp
        temp2 = temp1 * temp
        # short periodics
        mrt = rl * (1.0 - 1.5 * temp2 * betal * self.con41) + 0.5 * temp1 * self.x1mth2 * cos2u
        su = su - 0.25 * temp2 * self.x7thm1 * sin2u
        xnode = nodem + 1.5 * temp2 * self.cosio * sin2u
        xinc = self.inclo + 1.5 * temp2 * self.cosio * self.sinio * cos2u
        mvt = rdotl - nm * temp1 * self.x1mth2 * sin2u / _XKE
        rvdot = rvdotl + nm * temp1 * (self.x1mth2 * cos2u + 1.5 * self.con41) / _XKE
        if mrt < 1.0:
            raise ValueError("satellite has decayed")
        sinsu, cossu = math.sin(su), math.cos(su)
        snod, cnod = math.sin(xnode), math.cos(xnode)
        sini, cosi = math.sin(xinc), math.cos(xinc)
        xmx, xmy = -snod * cosi, cnod * cosi
        ux, uy, uz = xmx * sinsu + cnod * cossu, xmy * sinsu + snod * cossu, sini * sinsu
        vx, vy, vz = xmx * cossu - cnod * sinsu, xmy * cossu - snod * sinsu, sini * cossu
        vk = _RE * _XKE / 60.0
        r = (mrt * ux * _RE, mrt * uy * _RE, mrt * uz * _RE)
        v = ((mvt * ux + rvdot * vx) * vk, (mvt * uy + rvdot * vy) * vk, (mvt * uz + rvdot * vz) * vk)
        return r, v

    def at(self, unix_t):
        return self.propagate((unix_t - self.epoch) / 60.0)


def _unix_of_year(year):
    """Unix time of 1 January 00:00 UTC of that year."""
    days = (year - 1970) * 365 + (year - 1969) // 4 - (year - 1901) // 100 + (year - 1601) // 400
    return days * 86400.0


# ---------------------------------------------------------------- NASA ISS trajectory (CCSDS OEM, J2000)
def precession_matrix(unix_t):
    """IAU-76 precession, J2000 mean equator/equinox -> mean of date (rows of a 3x3 matrix)."""
    T = (unix_t / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    a = math.pi / (180 * 3600)
    zeta = (2306.2181 * T + 0.30188 * T * T + 0.017998 * T ** 3) * a
    z = (2306.2181 * T + 1.09468 * T * T + 0.018203 * T ** 3) * a
    th = (2004.3109 * T - 0.42665 * T * T - 0.041833 * T ** 3) * a
    cz, sz, cZ, sZ, ct, st = math.cos(zeta), math.sin(zeta), math.cos(z), math.sin(z), math.cos(th), math.sin(th)
    return ((cz * ct * cZ - sz * sZ, -sz * ct * cZ - cz * sZ, -st * cZ),
            (cz * ct * sZ + sz * cZ, -sz * ct * sZ + cz * cZ, -st * sZ),
            (cz * st, -sz * st, ct))


def _mul(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def _iso_unix(s):
    """'2026-11-05T12:00:00.000' (UTC) or '2026-309T12:00:00.000' -> unix seconds."""
    d, _, t = s.partition("T")
    hh, mm, ss = t.split(":")
    if d.count("-") == 1:                                # year-day of year
        y, doy = d.split("-")
        days = int(doy) - 1
    else:
        y, mo, dd = (int(x) for x in d.split("-"))
        cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
        days = cum[mo - 1] + dd - 1 + (1 if leap and mo > 2 else 0)
    return _unix_of_year(int(y)) + days * 86400 + int(hh) * 3600 + int(mm) * 60 + float(ss)


def parse_oem(text):
    """State vectors from a CCSDS OEM file: list of (unix, (x, y, z) km, (vx, vy, vz) km/s), J2000 frame.
    Header, comments and covariance blocks are skipped. Raises ValueError if the frame is not J2000/EME2000."""
    rows, frame, in_cov = [], "", False
    for line in (text or "").splitlines():
        ln = line.strip()
        if not ln or ln.startswith("COMMENT"):
            continue
        if ln.startswith("REF_FRAME"):
            frame = ln.split("=", 1)[1].strip().upper()
        if ln.startswith("COVARIANCE_START"):
            in_cov = True
        elif ln.startswith("COVARIANCE_STOP"):
            in_cov = False
            continue
        if in_cov:
            continue
        parts = ln.split()
        if len(parts) >= 7 and "T" in parts[0] and parts[0][:4].isdigit():
            try:
                rows.append((_iso_unix(parts[0]), tuple(float(x) for x in parts[1:4]),
                             tuple(float(x) for x in parts[4:7])))
            except ValueError:
                continue
    if frame and frame not in ("EME2000", "J2000", "ICRF"):
        raise ValueError(f"OEM frame {frame} not supported")
    rows.sort()
    return rows


class Ephemeris:
    """NASA's ISS trajectory, interpolated (8-point Lagrange) and precessed to the mean equator of date,
    which is close enough to SGP4's TEME (under 1 km) for passes and for checking the TLEs."""

    def __init__(self, rows, name="ISS (NASA)"):
        if len(rows) < 8:
            raise ValueError("NASA ephemeris has too few points")
        self.rows, self.name = rows, name
        self.times = [r[0] for r in rows]
        self.epoch = self.times[0]
        self.start, self.end = self.times[0], self.times[-1]

    def at(self, unix_t):
        import bisect
        if not self.start <= unix_t <= self.end:
            raise ValueError("time outside the NASA ephemeris")
        i = bisect.bisect_left(self.times, unix_t)
        lo = max(0, min(i - 4, len(self.rows) - 8))
        pts = self.rows[lo:lo + 8]
        r, v = [0.0] * 3, [0.0] * 3
        for j, (tj, rj, vj) in enumerate(pts):
            w = 1.0
            for k, (tk, _, _) in enumerate(pts):
                if k != j:
                    w *= (unix_t - tk) / (tj - tk)
            for n in range(3):
                r[n] += w * rj[n]
                v[n] += w * vj[n]
        p = precession_matrix(unix_t)
        return _mul(p, r), _mul(p, v)


def sat_name(sat):
    return getattr(sat, "name", "satellite")


def position_diff_km(a, b, unix_t):
    """Distance between where two orbit sources put the satellite at unix_t."""
    ra, _ = a.at(unix_t)
    rb, _ = b.at(unix_t)
    return math.dist(ra, rb)


def pick_best(sources, now=None):
    """The source with the newest orbit (a TLE's epoch; NASA's file counts as fresh while it covers now).
    sources: {label: Satellite or Ephemeris}. Returns (label, sat) or (None, None)."""
    now = time.time() if now is None else now

    def fresh(item):
        lab, s = item
        if isinstance(s, Ephemeris):
            return -1e18 if not s.start <= now <= s.end else now - 86400 * 2    # usable, but TLEs win when recent
        return s.epoch
    ok = [(k, v) for k, v in sources.items() if v is not None]
    return max(ok, key=fresh) if ok else (None, None)


# ---------------------------------------------------------------- where it is in your sky
def gmst(unix_t):
    """Greenwich mean sidereal time (IAU-82), radians."""
    jd = unix_t / 86400.0 + 2440587.5
    tut1 = (jd - 2451545.0) / 36525.0
    sec = (-6.2e-6 * tut1 ** 3 + 0.093104 * tut1 ** 2
           + (876600.0 * 3600.0 + 8640184.812866) * tut1 + 67310.54841)
    return math.radians(sec / 240.0) % _TWOPI


def observer_ecef(lat, lon, h_m=0.0):
    a, f = 6378.137, 1 / 298.257223563
    e2 = f * (2 - f)
    la, lo = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    h = h_m / 1000.0
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + h) * math.sin(la))


def look(sat, obs, unix_t):
    """(azimuth deg, elevation deg, range km) of the satellite from obs = (lat, lon[, height m])."""
    lat, lon = obs[0], obs[1]
    (x, y, z), _ = sat.at(unix_t)
    g = gmst(unix_t)
    cg, sg = math.cos(g), math.sin(g)
    xe, ye = cg * x + sg * y, -sg * x + cg * y             # TEME -> earth-fixed (polar motion ignored)
    ox, oy, oz = observer_ecef(lat, lon, obs[2] if len(obs) > 2 else 0.0)
    dx, dy, dz = xe - ox, ye - oy, z - oz
    la, lo = math.radians(lat), math.radians(lon)
    sla, cla, slo, clo = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    e = -slo * dx + clo * dy
    n = -sla * clo * dx - sla * slo * dy + cla * dz
    u = cla * clo * dx + cla * slo * dy + sla * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return (math.degrees(math.atan2(e, n)) % 360.0, math.degrees(math.atan2(u, math.hypot(e, n))), rng)


def elevation(sat, obs, t):
    return look(sat, obs, t)[1]


def _cross(sat, obs, t0, t1, rising):
    """Time (to ~1 s) where the elevation crosses 0 between t0 and t1."""
    for _ in range(20):
        if t1 - t0 < 1.0:
            break
        m = (t0 + t1) / 2
        above = elevation(sat, obs, m) >= 0
        if above == rising:
            t1 = m
        else:
            t0 = m
    return (t0 + t1) / 2


def _culmination(sat, obs, a, b):
    """Time of the highest elevation between AOS a and LOS b (golden-section search)."""
    gr = (math.sqrt(5) - 1) / 2
    c, d = b - gr * (b - a), a + gr * (b - a)
    fc, fd = elevation(sat, obs, c), elevation(sat, obs, d)
    while b - a > 1.0:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = elevation(sat, obs, c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = elevation(sat, obs, d)
    return (a + b) / 2


def passes(sat, obs, start=None, hours=24.0, min_el=0.0, step=30.0):
    """ISS passes over obs in the next `hours`: list of dicts with aos, tca, los (unix), max_el,
    aos_az, tca_az, los_az, duration (s). Passes whose highest point is below min_el are left out.
    A pass already in progress at `start` is included, with its true AOS."""
    start = time.time() if start is None else start
    end = start + hours * 3600.0
    out = []
    t = start
    prev_up = elevation(sat, obs, t) >= 0
    if prev_up:                                   # in progress: look back for its AOS
        b = t
        while elevation(sat, obs, b - step) >= 0 and t - b < 1800:
            b -= step
        aos = _cross(sat, obs, b - step, b, True)
    else:
        aos = None
    while t < end:
        t2 = t + step
        up = elevation(sat, obs, t2) >= 0
        if up and not prev_up:
            aos = _cross(sat, obs, t, t2, True)
        elif prev_up and not up and aos is not None:
            los = _cross(sat, obs, t, t2, False)
            tca = _culmination(sat, obs, aos, los)
            a_az, _, _ = look(sat, obs, aos)
            c_az, c_el, c_rng = look(sat, obs, tca)
            l_az, _, _ = look(sat, obs, los)
            if c_el >= min_el:
                out.append({"aos": aos, "tca": tca, "los": los, "max_el": round(c_el, 1),
                            "aos_az": round(a_az), "tca_az": round(c_az), "los_az": round(l_az),
                            "min_range_km": round(c_rng), "duration": round(los - aos)})
            aos = None
        prev_up = up
        t = t2
    return out


def window_above(sat, obs, p, el):
    """(start, end) of the part of pass p at or above `el` degrees, or None if it never gets that high."""
    if p["max_el"] < el:
        return None

    def at_el(a, b, rising):
        for _ in range(20):
            if b - a < 1.0:
                break
            m = (a + b) / 2
            if (elevation(sat, obs, m) >= el) == rising:
                b = m
            else:
                a = m
        return (a + b) / 2
    return at_el(p["aos"], p["tca"], True), at_el(p["tca"], p["los"], False)


COMPASS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass8(az):
    return COMPASS8[int((az + 22.5) // 45) % 8]


# ---------------------------------------------------------------- beacon schedule
def beacon_due(now, tca, el, min_el, pass_sent, recent_sent):
    """Should an ISS beacon go out now?
    tca          - time of the pass's highest point (before it = rising, after = setting)
    el           - current elevation (deg); beacons only at or above min_el
    pass_sent    - times of the beacons already sent on this pass
    recent_sent  - times of all ISS beacons (any pass), for the 5-in-5-minutes cap
    Returns (due, reason)."""
    if el < min_el:
        return False, "below minimum elevation"
    if sum(1 for t in recent_sent if now - t < WINDOW_S) >= WINDOW_MAX:
        return False, f"{WINDOW_MAX} beacons in the last {WINDOW_S // 60} minutes"
    if pass_sent and now - pass_sent[-1] < BEACON_GAP_S:
        return False, "less than a minute since the last one"
    if now >= tca and sum(1 for t in pass_sent if t >= tca) >= SETTING_MAX:
        return False, f"{SETTING_MAX} sent while setting"
    return True, "rising" if now < tca else "setting"


def plan(tca, start, end, min_gap=BEACON_GAP_S):
    """The beacon times beacon_due() gives for a pass whose usable window is start..end (for display
    and tests): every minute while rising, at most 2 while setting, never more than 5 in 5 minutes."""
    sent = []
    t = start
    while t <= end:
        ok, _ = beacon_due(t, tca, 90.0, 0.0, sent, sent)
        if ok:
            sent.append(t)
        t += 1.0
    return sent


def beacon_comment(cfg, seq, el):
    """Comment for an ISS position beacon (at most 43 characters). seq and the elevation make each
    beacon's text different, so the station's same-text limit does not hold them back."""
    base = (cfg.get("iss_comment") or "Tradewind via ISS").strip()
    tail = f" {cfg.get('grid', '')[:4].upper()} #{seq} el{int(round(el))}"
    return (base[:43 - len(tail)] + tail)[:43]
