########################################
#                                      #
#                                      #
#        install.py - Tony Ward        #
#                                      #
#   Installs Arch Linux how I like it  #
#                                      #
#                                      #
########################################

# Partition Scheme:
#       LVM on LUKS with encrypted efi partition
#       2 physical partitions - efi=512M, LUKS=rest of disk
#       3 LVM partitions on LUKS container - swap, root, home
#           Default, 16G, 128G, Rest of disk. Customisable

#!/usr/bin/python
from urllib import request
from install_util import *
from configparser import ConfigParser
import os
import subprocess
import re
import time
import getpass

# Constants for loading and validating install config
CONF_FILE = "config.txt"
CONFIG_HEADERS = {"Pacman.Pkgs", "Install.Config"}
CONFIG_FILE_SETTINGS = {"mount_path", "efi_size", "swap_size", "root_size", "luks_name", 
                        "volume_group", "hostname", "sudo_user", "tz.region", 
                        "tz.city", "locale", "language", "enable_services"}
CONFIG_RUNTIME_SETTINGS = {"install_disk", "partitions.phys.efi", "partitions.phys.luks",
                           "partitions.lvm.swap", "partitions.lvm.root", "partitions.lvm.home"}

# Positions of useful information from lsblk command
# lsblk output: <NAME>  <MAJ:MIN>   <RM>    <SIZE>  <RO>    <TYPE>    <MOUNTPOINTS>
NAME_INDEX = 0
SIZE_INDEX = 3
TYPE_INDEX = 5

# Input string to create 2 partitions with sfdisk
# Format position is for efi partition size
# sfdisk stdin - 1 line per partition <start>, <size>, <type> - blank is default, U is efi 
SFDISK_PART = ", {}, U\n,,"

# Repo to install yay
YAY_REPO = "https://aur.archlinux.org/yay.git"

def main():
    log("[*] Install commencing")

    if not has_network():
        exit()
    time.sleep(1)
    
    try:
        installer = Installer(CONF_FILE)
        installer.full_install()
    except Exception as e:
        log("[!] {}".format(e))


class Installer:
    """Holds installation config and executes discrete installation steps"""
    def __init__(self, config_path):
        """Read and validate config from provided file"""
        if not os.path.isfile(config_path):
            raise Exception("{} does not exist".format(config_path))

        config_file = ConfigParser(allow_no_value=True)
        config_file.read(config_path)

        for header in CONFIG_HEADERS:
            if not header in config_file.sections():
                raise Exception("Header {} not found in config file".format(header))

        self.pacman_pkgs = " ".join([pkg for pkg in config_file["Pacman.Pkgs"]])
        self.yay_pkgs = " ".join([pkg for pkg in config_file["Yay.Pkgs"]])
        self.config = config_file["Install.Config"]

        # Contents of keys is not validated
        for key in CONFIG_FILE_SETTINGS:
            if not key in self.config:
                raise Exception("Key {} missing from Install.Config section of config".format(key))

        # Come configs must be set at runtime, rather than in config file
        # Make them blank for now so methods don't have to check for keys
        for key in CONFIG_RUNTIME_SETTINGS:
            self.config[key] = ""

    def full_install(self):
        try:
            self.select_disk()
            self.partition_disk_phys()
            self.encrypt_luks_partition()
            self.create_lvm_partitions()
            self.format_partitions()
            self.mount_partitions()
            self.pacstrap()
            self.conf_fstab()
            self.conf_tz()
            self.conf_locale()
            self.conf_network()
            self.conf_users()
            self.install_grub()
            self.enable_services()
            self.install_yay()
            self.install_yay_pkgs()
        except Exception as e:
            raise e

    def select_disk(self):
        """Prompt user to select installation disk""" 
        proc = execute("lsblk -p")
        result = proc.stdout
        
        # Create list of tuples (disk_path, disk_size)
        disks = []
        for line in result.splitlines():
            line = line.split()
            if line[TYPE_INDEX] == "disk":
                disks.append((line[NAME_INDEX], line[SIZE_INDEX]))

        # Display available disks and select disk to install to
        for i in range(len(disks)): 
            print("{}\t{}".format(disks[i][0], disks[i][1]))

        valid = False
        install_disk = ""
        while not valid:
            install_disk = input("Please select a disk to install to: ")
            for disk in disks:
                if install_disk == disk[0]:
                    valid = True
            
            if not valid:
                log("[!] Invalid disk selected, please try again")
        
        self.config["install_disk"] = install_disk
    
        # Assumption: install_disk + p1 is efi install_disk + p2 is luks
        self.config["partitions.phys.efi"] = install_disk + "p1"
        self.config["partitions.phys.luks"] = install_disk + "p2"
    
    def partition_disk_phys(self):
        """Creates 2 partitions, efi and LUKS"""    
        disk = self.config["install_disk"]
    
        if not os.path.exists(disk):
            raise Exception("Disk not found - {}".format(disk))

        log("[*] Clearing any existing partition table")
        execute("sfdisk --delete {}".format(disk))

        log("[*] Creating efi and LUKS partitions")
        cmd_input = SFDISK_PART.format(self.config["efi_size"]) 
        execute("sfdisk {}".format(disk), stdin=cmd_input)

    def encrypt_luks_partition(self): 
        """Encrypt LUKS partition using Luks1 for compatability with grub"""
        luks_partition = self.config["partitions.phys.luks"]
        luks_name = self.config["luks_name"]

        if not os.path.exists(luks_partition):
            raise Exception("Partition not found - {}".format(luks_partition))
        if luks_name == "":
            raise Exception("Luks name not set")

        log("[*] Encrpyting {}".format(luks_partition))
        # Use luks1 for grub compatability
        execute("cryptsetup luksFormat --type luks1 {}".format(luks_partition), interactive=True)

        log("[*] Opening LUKS container to partition with LVM")
        execute("cryptsetup open {} {}".format(luks_partition, luks_name), interactive=True)

    def create_lvm_partitions(self):
        """Creates LVM partitions, requires an open LUKS container"""
        swap_size = self.config["swap_size"]
        root_size = self.config["root_size"]
        vol_grp   = self.config["volume_group"]
        luks_path = "/dev/mapper/{}".format(self.config["luks_name"])

        if not os.path.exists(luks_path):
            raise Exception("{} does not exist".format(luks_path))

        log("[*] Creating LVM volumes")
        execute("pvcreate {}".format(luks_path))
        execute("vgcreate {} {}".format(vol_grp, luks_path))
        execute("lvcreate -L {} {} -n swap".format(swap_size, vol_grp))
        execute("lvcreate -L {} {} -n root".format(root_size, vol_grp))
        # home gets all space not used by swap or root
        execute("lvcreate -l 100%FREE {} -n home".format(vol_grp))

        self.config["partitions.lvm.root"] = "/dev/{}/root".format(vol_grp)
        self.config["partitions.lvm.home"] = "/dev/{}/home".format(vol_grp)
        self.config["partitions.lvm.swap"] = "/dev/{}/swap".format(vol_grp)
        
    def format_partitions(self):
        """Format efi, home, and root. mkswap swap"""
        efi = self.config["partitions.phys.efi"]
        root = self.config["partitions.lvm.root"]
        home = self.config["partitions.lvm.home"]
        swap = self.config["partitions.lvm.swap"]

        try:
            validate_file_paths([efi, root, home, swap])
        except Exception as e:
            raise e

        log("[*] Formatting partitions")

        execute("mkfs.ext4 {}".format(root))
        execute("mkfs.ext4 {}".format(home))
        execute("mkswap {}".format(swap))
        execute("mkfs.fat -F32 {}".format(efi))

    def mount_partitions(self):
        """Mount efi, home, and root. swapon swap"""
        mnt_path = self.config["mount_path"]
        efi = self.config["partitions.phys.efi"]
        root = self.config["partitions.lvm.root"]
        home = self.config["partitions.lvm.home"]
        swap = self.config["partitions.lvm.swap"]

        try:
            validate_file_paths([efi, root, home, swap])
        except Exception as e:
            raise e

        log("[*] Mounting partitions")
    
        execute("mount {} {}".format(root, mnt_path))
        execute("mkdir {}/efi".format(mnt_path))
        execute("mkdir {}/home".format(mnt_path))
        execute("mount {} {}/efi".format(efi, mnt_path))
        execute("mount {} {}/home".format(home, mnt_path))
        execute("swapon {}".format(swap))

    def pacstrap(self):
        """Install arch linux and any specified packages (Pacman not AUR)"""
        mnt_path = self.config["mount_path"]
        packages = self.pacman_pkgs
        log("[*] Running pacstrap to install base system")
        execute("pacstrap {} {}".format(mnt_path, packages), interactive=True)
 
    def conf_fstab(self):
        """Creates fstab on new install"""
        mnt_path = self.config["mount_path"]
        fstab_file = "{}/etc/fstab".format(mnt_path)
        log("[*] Configuring fstab")
        execute("genfstab -U {}".format(mnt_path), outfile=fstab_file)
 
    def conf_tz(self):
        """Links specified timzone file to /etc/localtime"""
        mnt_path = self.config["mount_path"]
        region = self.config["tz.region"]
        city = self.config["tz.city"]

        tz_file = "{}/usr/share/zoneinfo/{}/{}".format(mnt_path, region, city)
        localtime_file = "{}/etc/localtime".format(mnt_path)

        log("[*] Setting timezone")
        execute("ln -sf {} {}".format(tz_file, localtime_file))
 
    def conf_locale(self):
        """Creates /etc/locale.gen and /etc/locale.conf"""
        mnt_path = self.config["mount_path"]
        locale = self.config["locale"]
        lang = "LANG={}".format(self.config["language"])

        log("[*] Configuring localization settings")
        write_file(locale, "{}/etc/locale.gen".format(mnt_path))
        write_file(lang, "{}/etc/locale.conf".format(mnt_path))
        execute("locale-gen", chroot_dir=mnt_path)

    def conf_network(self):
        """Creates /etc/hosts and /etc/hostname using provided hostname"""
        mnt_path = self.config["mount_path"]
        hostname = self.config["hostname"]

        log("[*] Configuring network hosts")
        write_file(hostname, "{}/etc/hostname".format(mnt_path))
        hosts = "127.0.0.1\tlocalhost\n127.0.0.1\t{}".format(hostname)
        write_file(hosts, "{}/etc/hosts".format(mnt_path))

    def conf_users(self):
        """Prompts for root password, edits sudoers file, creates sudo user"""
        mnt_path = self.config["mount_path"]
        sudo_user = self.config["sudo_user"]

        log("[*] Configuring users")
        log("[+] Set root password")
        execute("passwd root", chroot_dir=mnt_path, interactive=True)

        log("[+] Creating sudo user")
        execute("useradd -mG wheel {}".format(sudo_user), chroot_dir=mnt_path)
        execute("passwd {}".format(sudo_user), chroot_dir=mnt_path)
        sudoers = "root ALL=(ALL:ALL) ALL\n" + "%wheel ALL=(ALL:ALL) ALL\n" + "@includedir /etc/sudoers.d"
        write_file(sudoers, "{}/etc/sudoers".format(mnt_path))

    def install_grub(self):
        """
        Creates master encryption key foor grub to boot with
        Edits required config files (/etc/mkinitcpio.conf, /etc/default/grub)
        Creates initram image, and installs and configures grub
        """
        mnt_path = self.config["mount_path"]
        luks_name = self.config["luks_name"]
        luks_partition = self.config["partitions.phys.luks"]
        initram_hooks = self.config["initram_hooks"]

        log ("[*] Creating grub encryption key")
        key_path = "{}/root/{}.keyfile".format(mnt_path, luks_name)
        print(key_path)
        execute("dd bs=512 count=4 if=/dev/random of={} iflag=fullblock".format(key_path))
        execute("chmod 000 {}".format(key_path))
        execute("cryptsetup -v luksAddKey {} {}".format(luks_partition, key_path), interactive=True)
    
        log("[*] Configuring intram with encrypted boot")
        initram = "{}\nFILES=(/root/{}.keyfile)".format(initram_hooks, luks_name)
        write_file(initram, "{}/etc/mkinitcpio.conf".format(mnt_path))
        execute("mkinitcpio -P", chroot_dir=mnt_path)
        execute("chmod 600 {}/boot/initramfs-linux*".format(mnt_path))

        log("[*] Installing grub")
        grub_file = "{}/etc/default/grub".format(mnt_path)
        grub_cmdline = "GRUB_CMDLINE_LINUX=\"cryptdevice={}:{} cryptkey=rootfs:/root/{}.keyfile\"\n".format(luks_partition, luks_name, luks_name)
        grub_crypt = "GRUB_ENABLE_CRYPTODISK=y\n"
        replace_in_file(".*GRUB_CMDLINE_LINUX=.*", grub_cmdline, grub_file)
        replace_in_file(".*GRUB_ENABLE_CRYPTODISK.*", grub_crypt, grub_file)
        execute("grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=GRUB", chroot_dir=mnt_path)
        execute("grub-mkconfig -o /boot/grub/grub.cfg", chroot_dir=mnt_path)

    def enable_services(self):
        """Enables all required services"""
        mnt_path = self.config["mount_path"]
        services = self.config["enable_services"].split()

        log("[*] Enabling services")
        for serv in services:
            execute("systemctl enable {}".format(serv), chroot_dir=mnt_path)

    # Use of arch-chroot is hacky and inconsistent :(
    def install_yay(self):
        """Clones yay from AUR and installs on guest system"""
        mnt_path = self.config["mount_path"]
        sudo_user = self.config["sudo_user"]

        # makepkg must be run as non-root user and from dir of pkg being installed
        yay_dir = "/home/{}/yay".format(sudo_user)

        su = "su {}".format(sudo_user)
        clone_repo = "git clone {} {}".format(YAY_REPO, yay_dir)
        build_yay = "cd {}; makepkg -si --noconfirm".format(yay_dir)

        execute(su, stdin=clone_repo, chroot_dir=mnt_path)
        execute(su, stdin=build_yay, chroot_dir=mnt_path, interactive=True)

    def install_yay_pkgs(self):
        """Installs packages using yay"""
        mnt_path =self.config["mount_path"]
        sudo_user = self.config["sudo_user"]
        yay_pkgs = self.yay_pkgs

        # yay cannot be run as root
        su = "su {}".format(sudo_user)
        yay_cmd = "yay -Sy --noconfirm"

        execute(su, stdin=yay_cmd, chroot_dir=mnt_path, interactive=True)

    def configure():
        """Unimplemented"""
        return

if __name__ == "__main__":
    main()


