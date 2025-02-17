# SPDX-License-Identifier: MIT
import os, os.path, plistlib, shutil, sys, stat, subprocess, urlcache, zipfile, logging
import osenum, firmware.wifi
from util import *

class StubInstaller(PackageInstaller):
    def __init__(self, sysinfo, dutil, osinfo, ipsw_info):
        super().__init__()
        self.dutil = dutil
        self.sysinfo = sysinfo
        self.osinfo = osinfo
        self.install_version = ipsw_info.version.split(maxsplit=1)[0]
        self.ucache = None

        base = os.environ.get("IPSW_BASE", None)
        url = ipsw_info.url
        if base:
            url = base + "/" + os.path.split(url)[-1]

        logging.info(f"IPSW URL: {url}")
        if url.startswith("http"):
            print("Downloading macOS OS package info...")
            self.ucache = urlcache.URLCache(url)
            self.pkg = zipfile.ZipFile(self.ucache)
        else:
            print("Loading macOS OS package info...")
            self.pkg = zipfile.ZipFile(open(url, "rb"))
        self.flush_progress()
        logging.info(f"OS package opened")
        print()

    def prepare_volume(self, part):
        logging.info(f"StubInstaller.prepare_volume({part.name=!r})")
        self.part = part

        by_role = {}

        ctref = self.part.container["ContainerReference"]

        print("Preparing target volumes...")
        logging.info("Preparing target volumes")

        for volume in self.part.container["Volumes"]:
            roles = tuple(volume["Roles"])
            logging.info(f" {volume['DeviceIdentifier']} roles: {roles}")
            by_role.setdefault(roles, []).append(volume)

        for role in ("Preboot", "Recovery", "Data", "System"):
            vols = by_role.get(role, [])
            if len(vols) > 1:
                raise Exception(f"Multiple {role} volumes")

        self.label = self.part.label or "Linux"
        if not by_role.get(("Data",), None):
            if default_vol := by_role.get((), None):
                self.dutil.changeVolumeRole(default_vol[0]["DeviceIdentifier"], "D")
                self.dutil.rename(default_vol[0]["DeviceIdentifier"], self.label + " - Data")
            else:
                self.dutil.addVolume(ctref, self.label, role="D")
            self.dutil.refresh_part(self.part)
        else:
            self.label = self.label.rstrip(" - Data")

        for volume in self.part.container["Volumes"]:
            if volume["Roles"] == ["Data",]:
                data_volume = volume["DeviceIdentifier"]
                break
        else:
            raise Exception("Could not find Data volume")

        if not by_role.get(("System",), None):
            self.dutil.addVolume(ctref, self.label, role="S", groupWith=data_volume)

        if not by_role.get(("Preboot",), None):
            self.dutil.addVolume(ctref, "Preboot", role="B")

        if not by_role.get(("Recovery",), None):
            self.dutil.addVolume(ctref, "Recovery", role="R")

        self.dutil.refresh_part(self.part)

    def check_volume(self, part=None):
        if part:
            self.part = part

        logging.info(f"StubInstaller.check_volume({self.part.name=!r})")

        print("Checking volumes...")
        os = self.osinfo.collect_part(self.part)

        if len(os) != 1:
            raise Exception("Container is not ready for OS install")

        self.osi = os[0]

    def chflags(self, flags, path):
        logging.info(f"chflags {flags} {path}")
        subprocess.run(["chflags", flags, path], check=True)

    def install_files(self, cur_os):
        logging.info("StubInstaller.install_files()")
        logging.info(f"VGID: {self.osi.vgid}")
        logging.info(f"OS info: {self.osi}")

        print("Beginning stub OS install...")
        ipsw = self.pkg

        logging.info("Parsing metadata...")

        sysver = plistlib.load(ipsw.open("SystemVersion.plist"))
        manifest = plistlib.load(ipsw.open("BuildManifest.plist"))
        bootcaches = plistlib.load(ipsw.open("usr/standalone/bootcaches.plist"))
        self.flush_progress()

        for identity in manifest["BuildIdentities"]:
            if (identity["ApBoardID"] != f'0x{self.sysinfo.board_id:02X}' or
                identity["ApChipID"] != f'0x{self.sysinfo.chip_id:04X}' or
                identity["Info"]["DeviceClass"] != self.sysinfo.device_class or
                identity["Info"]["RestoreBehavior"] != "Erase" or
                identity["Info"]["Variant"] != "macOS Customer"):
                continue
            break
        else:
            raise Exception("Failed to locate a usable build identity for this device")

        logging.info(f'Using OS build {identity["Info"]["BuildNumber"]} for {self.sysinfo.device_class}')

        manifest["BuildIdentities"] = [identity]

        print("Setting up System volume...")
        logging.info("Setting up System volume")

        self.extract("usr/standalone/bootcaches.plist", self.osi.system)
        shutil.copy("logo.icns", os.path.join(self.osi.system, ".VolumeIcon.icns"))

        cs = os.path.join(self.osi.system, "System/Library/CoreServices")
        os.makedirs(cs, exist_ok=True)
        sysver["ProductUserVisibleVersion"] += " (stub)"
        self.extract("PlatformSupport.plist", cs)
        self.flush_progress()

        # Make the icon work
        try:
            logging.info(f"xattr -wx com.apple.FinderInfo .... {self.osi.system}")
            subprocess.run(["xattr", "-wx", "com.apple.FinderInfo",
                           "0000000000000000040000000000000000000000000000000000000000000000",
                           self.osi.system], check=True)
        except:
            print("Failed to apply extended attributes, logo will not work.")

        print("Setting up Data volume...")
        logging.info("Setting up Data volume")

        os.makedirs(os.path.join(self.osi.data, "private/var/db/dslocal"), exist_ok=True)

        print("Setting up Preboot volume...")
        logging.info("Setting up Preboot volume")

        pb_vgid = os.path.join(self.osi.preboot, self.osi.vgid)
        os.makedirs(pb_vgid, exist_ok=True)

        bless2 = bootcaches["bless2"]

        restore_bundle = os.path.join(pb_vgid, bless2["RestoreBundlePath"])
        os.makedirs(restore_bundle, exist_ok=True)
        with open(os.path.join(restore_bundle, "BuildManifest.plist"), "wb") as fd:
            plistlib.dump(manifest, fd)
        self.extract("SystemVersion.plist", restore_bundle)
        self.extract("RestoreVersion.plist", restore_bundle)
        self.extract("usr/standalone/bootcaches.plist", restore_bundle)

        self.extract_tree("BootabilityBundle/Restore/Bootability",
                          os.path.join(restore_bundle, "Bootability"))
        self.extract_file("BootabilityBundle/Restore/Firmware/Bootability.dmg.trustcache",
                          os.path.join(restore_bundle, "Bootability/Bootability.trustcache"))

        self.extract_tree("Firmware/Manifests/restore/macOS Customer/", restore_bundle)

        copied = set()
        for key, val in identity["Manifest"].items():
            if key in ("BaseSystem", "OS", "Ap,SystemVolumeCanonicalMetadata"):
                continue
            path = val["Info"]["Path"]
            if path in copied:
                continue
            self.extract(path, restore_bundle)
            copied.add(path)

        self.flush_progress()

        os.makedirs(os.path.join(pb_vgid, "var/db"), exist_ok=True)
        admin_users = os.path.join(cur_os.preboot, cur_os.vgid, "var/db/AdminUserRecoveryInfo.plist")
        tg_admin_users = os.path.join(pb_vgid, "var/db/AdminUserRecoveryInfo.plist")

        if os.path.exists(tg_admin_users):
            self.chflags("noschg", tg_admin_users)
        shutil.copy(admin_users, tg_admin_users)

        # Stop macOS <12.0 bootability stufff from clobbering this file
        self.chflags("schg", tg_admin_users)

        # This is a workaround for some screwiness in the macOS <12.0 bootability
        # code, which ends up putting the apticket in the wrong volume...
        sys_restore_bundle = os.path.join(self.osi.system, bless2["RestoreBundlePath"])
        if os.path.lexists(sys_restore_bundle):
            os.unlink(sys_restore_bundle)
        os.symlink(restore_bundle, sys_restore_bundle)

        print("Setting up Recovery volume...")
        logging.info("Setting up Recovery volume")

        rec_vgid = os.path.join(self.osi.recovery, self.osi.vgid)
        os.makedirs(rec_vgid, exist_ok=True)

        basesystem_path = os.path.join(rec_vgid, "usr/standalone/firmware")
        os.makedirs(basesystem_path, exist_ok=True)

        logging.info("Extracting arm64eBaseSystem.dmg")
        self.extract_file(identity["Manifest"]["BaseSystem"]["Info"]["Path"],
                          os.path.join(basesystem_path, "arm64eBaseSystem.dmg"))
        self.flush_progress()

        self.systemversion_path = os.path.join(cs, "SystemVersion.plist")

        print("Wrapping up...")

        logging.info("Writing SystemVersion.plist")
        with open(self.systemversion_path, "wb") as fd:
            plistlib.dump(sysver, fd)

        logging.info("Copying Finish Installation.app")
        shutil.copytree("step2/Finish Installation.app",
                        os.path.join(self.osi.system, "Finish Installation.app"))

        logging.info("Writing step2.sh")
        step2_sh = open("step2/step2.sh").read().replace("##VGID##", self.osi.vgid)
        resources = os.path.join(self.osi.system, "Finish Installation.app/Contents/Resources")
        step2_sh_dst = os.path.join(resources, "step2.sh")
        with open(step2_sh_dst, "w") as fd:
            fd.write(step2_sh)
        os.chmod(step2_sh_dst, 0o755)
        self.step2_sh = step2_sh_dst
        self.boot_obj_path = os.path.join(resources, "boot.bin")

        logging.info("Copying .IAPhysicalMedia")
        shutil.copy("step2/IAPhysicalMedia.plist",
                    os.path.join(self.osi.system, ".IAPhysicalMedia"))

        print("Stub OS installation complete.")
        logging.info("Stub OS installed")
        print()

    def collect_firmware(self, pkg):
        print("Collecting firmware...")
        logging.info("StubInstaller.collect_firmware()")

        img = os.path.join(self.osi.recovery, self.osi.vgid,
                           "usr/standalone/firmware/arm64eBaseSystem.dmg")
        logging.info("Attaching recovery ramdisk")
        subprocess.run(["hdiutil", "attach", "-quiet", "-readonly", "-mountpoint", "recovery", img],
                       check=True)
        logging.info("Collecting WiFi firmware")
        col = firmware.wifi.WiFiFWCollection("recovery/usr/share/firmware/wifi/")
        pkg.add_files(sorted(col.files()))
        logging.info("Detaching recovery ramdisk")
        subprocess.run(["hdiutil", "detach", "recovery"])
