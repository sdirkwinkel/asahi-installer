# SPDX-License-Identifier: MIT
import os, os.path, plistlib, subprocess, logging
from dataclasses import dataclass

UUID_SROS = "3D3287DE-280D-4619-AAAB-D97469CA9C71"
UUID_FROS = "C8858560-55AC-400F-BBB9-C9220A8DAC0D"

@dataclass
class OSInfo:
    partition: object
    vgid: str
    label: str = None
    sys_volume: str = None
    data_volume: str = None
    stub: bool = False
    version: str = None
    m1n1_ver: str = None
    system: object = None
    data: object = None
    preboot: object = None
    recovery: object = None
    rec_vgid: str = None
    bp: object = None

    def __str__(self):
        if self.vgid == UUID_SROS:
            return f"recoveryOS v{self.version} [Primary recoveryOS]"
        elif self.vgid == UUID_FROS:
            return f"recoveryOS v{self.version} [Fallback recoveryOS]"
        elif not self.stub:
            if self.m1n1_ver is not None:
                return f"[{self.label}] macOS v{self.version} + m1n1 {self.m1n1_ver} [{self.sys_volume}, {self.vgid}]"
            elif self.bp and self.bp.get("coih", None):
                return f"[{self.label}] macOS v{self.version} + unknown fuOS [{self.sys_volume}, {self.vgid}]"
            else:
                return f"[{self.label}] macOS v{self.version} [{self.sys_volume}, {self.vgid}]"
        elif self.bp and self.bp.get("coih", None):
            if self.m1n1_ver:
                return f"[{self.label}] m1n1 v{self.m1n1_ver} (macOS {self.version} stub) [{self.sys_volume}, {self.vgid}]"
            else:
                return f"[{self.label}] unknown fuOS (macOS {self.version} stub) [{self.sys_volume}, {self.vgid}]"
        else:
            return f"[{self.label}] incomplete install (macOS {self.version} stub) [{self.sys_volume}, {self.vgid}]"

class OSEnum:
    def __init__(self, sysinfo, dutil, sysdsk):
        self.sysinfo = sysinfo
        self.dutil = dutil
        self.sysdsk = sysdsk

    def collect(self, parts):
        logging.info("OSEnum.collect()")
        for p in parts:
            p.os = []
            if p.type == "Apple_APFS_Recovery":
                self.collect_recovery(p)
            else:
                self.collect_part(p)

    def collect_recovery(self, part):
        logging.info(f"OSEnum.collect_recovery(part={part.name})")
        recs = []

        for volume in part.container["Volumes"]:
            if volume["Roles"] == ["Recovery"]:
                recs.append(volume)

        if len(recs) != 1:
            return

        os = OSInfo(partition=part, vgid=UUID_SROS,
                    rec_vgid=recs[0]["APFSVolumeUUID"],
                    version=self.sysinfo.sfr_ver)
        logging.info(f" Found SROS: {os}")
        part.os.append(os)
        if self.sysinfo.fsfr_ver:
            os = OSInfo(partition=part, vgid=UUID_FROS,
                        version=self.sysinfo.fsfr_ver)
            logging.info(f" Found FROS: {os}")
            part.os.append(os)

    def collect_part(self, part):
        logging.info(f"OSEnum.collect_part(part={part.name})")
        if part.container is None:
            return

        part.os = []

        ct = part.container
        by_role = {}
        by_device = {}

        for volume in ct["Volumes"]:
            by_role.setdefault(tuple(volume["Roles"]), []).append(volume)
            by_device[volume["DeviceIdentifier"]] = volume

        volumes = {}

        for role in ("Preboot", "Recovery"):
            vols = by_role.get((role,), None)
            if not vols:
                logging.info(f" No {role} volume")
                return
            elif len(vols) > 1:
                logging.info(f"  Multiple {role} volumes ({vols})")
                return
            volumes[role] = vols[0]

        for vg in ct["VolumeGroups"]:
            data = [i for i in vg["Volumes"] if i["Role"] == "Data"]
            system = [i for i in vg["Volumes"] if i["Role"] == "System"]
            if len(data) != 1 or len(system) != 1:
                logging.info(f"  Weird VG: {vg['Volumes']}")
                continue

            volumes["Data"] = by_device[data[0]["DeviceIdentifier"]]
            volumes["System"] = by_device[system[0]["DeviceIdentifier"]]
            vgid = vg["APFSVolumeGroupUUID"]
            os = self.collect_os(part, volumes, vgid)
            logging.info(f" Found {os}")
            part.os.append(os)

        return part.os

    def collect_os(self, part, volumes, vgid):
        logging.info(f"OSEnum.collect_os(part={part.name}, vgid={vgid})")
        mounts = {}

        for role in ("Preboot", "Recovery", "System"):
            mounts[role] = self.dutil.mount(volumes[role]["DeviceIdentifier"])

        # Data will fail to mount for FileVault-enabled OSes; ignore that.
        try:
            mounts["Data"] = self.dutil.mount(volumes["Data"]["DeviceIdentifier"])
        except:
            mounts["Data"] = None

        rec_vgid = volumes["Recovery"]["APFSVolumeUUID"]

        stub = not os.path.exists(os.path.join(mounts["System"], "Library"))

        sys_volume = volumes["System"]["DeviceIdentifier"]
        label = volumes["System"]["Name"]

        osi = OSInfo(partition=part, vgid=vgid, stub=stub, label=label,
                     sys_volume=sys_volume,
                     system=mounts["System"],
                     data=mounts["Data"],
                     preboot=mounts["Preboot"],
                     recovery=mounts["Recovery"],
                     rec_vgid=rec_vgid)

        for name in ("SystemVersion.plist", "SystemVersion-disabled.plist"):
            try:
                sysver = plistlib.load(open(os.path.join(mounts["System"],
                    "System/Library/CoreServices", name), "rb"))
                osi.version = sysver["ProductVersion"]
            except FileNotFoundError:
                continue

        try:
            bps = self.bputil("-d", "-v", vgid)
        except subprocess.CalledProcessError:
            logging.info(f"  bputil failed")
            return osi

        osi.bp = {}
        for k in ("coih", "nsih"):
            tag = f"({k}): ".encode("ascii")
            if tag in bps:
                val = bps.split(tag)[1].split(b"\n")[0].decode("ascii")
                if val == "absent":
                    val = None
                osi.bp[k] = val
                logging.info(f"  BootPolicy[{k}] = {val}")

        if coih := osi.bp.get("coih", None):
            fuos_path = os.path.join(mounts["Preboot"], vgid, "boot",
                                     osi.bp["nsih"],
                                     "System/Library/Caches/com.apple.kernelcaches",
                                     "kernelcache.custom." + coih)
            fuos = open(fuos_path, "rb").read()
            if b"##m1n1_ver##" in fuos:
                osi.m1n1_ver = fuos.split(b"##m1n1_ver##")[1].split(b"\0")[0].decode("ascii")
                logging.info(f"  m1n1 version found: {osi.m1n1_ver}")

        return osi

    def bputil(self, *args):
        result = subprocess.run(["bputil"] + list(args),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True)
        return result.stdout
