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
INSTALL_CONFIG_KEYS = {"luks_name", "tz.region", "tz.city", "hostname", 
                       "mount_path", "sudo_user"}

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
    
    config = parse_config(CONF_FILE)

    install_disk = select_disk() 

    partition_disk_phys(install_disk)
    # Slight assumption, first volume made is <disk>+p1, second is <disk>+p2
    efi_partition = install_disk + "p1"
    luks_partition = install_disk + "p2"

    encrypt_partition(luks_partition)
    
    create_lvm_on_luks()
    
    partitions = {"root": "/dev/vg0/root", "home": "/dev/vg0/home", 
                  "swap": "/dev/vg0/swap", "efi": efi_partition}
    format_partitions(partitions)
    mount_partitions(partitions)

    pacstrap(config["pacman_pkgs"])
    
    conf_fstab()
    conf_tz()
    conf_locale()
    conf_network()
    conf_users()
    install_grub(luks_partition)
    enable_services()

class Installer:
    """Holds installation config and executes discrete installation steps"""
    def __init__(self, config_path):
        """Read and validate config from provided file"""
        if not os.path.isfile(config_path):
            raise Exception("{} does not exist".format(config_path))

        self.config = ConfigParser(allow_no_value=True)
        self.config.read(config_path)

        for header in CONFIG_HEADERS:
            if not header in self.config.sections():
                raise Exception("Header {} not found in config file".format(header))

        for key in INSTALL_CONFIG_KEYS:
            if not key in self.config["Install.Config"]:
                raise Exception("Key {} missing from Install.Config section of config".format(key))

        # select_disk must be called before these values used
        # Setting these as an empty string now just makes some checks simpler
        self.config["Install.Config"]["install_disk"] = ""
        self.config["Install.Config"]["efi_partition"] = ""
        self.config["Install.Config"]["luks_partition"] = ""

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
                else:
                    log("[!] Invalid disk selected, please try again")
        
        self.config["Install.Config"]["install_disk"] = install_disk
    
        # Assumption: install_disk + p1 is efi install_disk + p2 is luks
        self.config["Install.Config"]["efi_partition"] = install_disk + "p1"
        self.config["Install.Config"]["luks_partition"] = install_disk + "p2"
    
    def partition_disk_phys():
        """Creates 2 partitions, efi and LUKS"""    
        disk = self.config["Install.Config"]["install_disk"]
    
        if not os.path.exists(disk):
            raise Exception("Disk not found - {}".format(disk))

        log("[*] Clearing any existing partition table")
        execute("sfdisk --delete {}".format(disk))

        log("[*] Creating efi and LUKS partitions")
        cmd_input = SFDISK_PART.format(self.config["Install.Config"]["efi_size"])
        execute("sfdisk {}".format(disk), stdin=cmd_input)

    def encrypt_luks_partition():
        luks_partition = self.config["Install.Config"]["luks_partition"]
        luks_name = self.config["Install.Config"]["luks_name"]

        if not os.path.exists(luks_partition):
            raise Exception("Partition not found - {}".format(luks_partition))
        if luks_name == "":
            raise Exception("Luks name not set")

        log("[*] Encrpyting {}".format(luks_partition))
        # Use luks1 for grub compatability
        execute("cryptsetup luksFormat --type luks1 {}".format(luks_partition), interactive=True)

        log("[*] Opening LUKS container to partition with LVM")
        execute("cryptsetup open {} {}".format(luks_partition, luks_name), interactive=True)

def create_lvm_on_luks(luks="cryptlvm", vol_grp="vg0", swap_size="16G", root_size="128G"):
    log("[*] Creating LVM volumes")
    execute("pvcreate /dev/mapper/{}".format(luks))
    execute("vgcreate {} /dev/mapper/{}".format(vol_grp, luks))
    execute("lvcreate -L {} {} -n swap".format(swap_size, vol_grp))
    execute("lvcreate -L {} {} -n root".format(root_size, vol_grp))
    # home gets all space not used by swap or root
    execute("lvcreate -l 100%FREE {} -n home".format(vol_grp))

def format_partitions(partitions):
    log("[*] Formatting partitions")
    if not has_part_paths(partitions):
        exit()

    execute("mkfs.ext4 {}".format(partitions["root"]))
    execute("mkfs.ext4 {}".format(partitions["home"]))
    execute("mkswap {}".format(partitions["swap"]))
    execute("mkfs.fat -F32 {}".format(partitions["efi"]))

def mount_partitions(partitions, mnt_pnt="/mnt"):
    log("[*] Mounting partitions")
    if not has_part_paths(partitions):
        exit()
    
    execute("mount {} {}".format(partitions["root"], mnt_pnt))
    execute("mkdir {}/efi".format(mnt_pnt))
    execute("mkdir {}/home".format(mnt_pnt))
    execute("mount {} {}/efi".format(partitions["efi"], mnt_pnt))
    execute("mount {} {}/home".format(partitions["home"], mnt_pnt))
    execute("swapon {}".format(partitions["swap"]))

# Checks that partitions contains a path for root, home, swap and efi
def has_part_paths(partitions):
    for part in ["root", "home", "swap", "efi"]:
        if part not in partitions:
            log("[!] Path not provided for all partitions")
            return False
    return True

def pacstrap(packages, mnt_path="/mnt"):
    log("[*] Running pacstrap to install base system")
    execute("pacstrap {} {}".format(mnt_path, packages), interactive=True)

def conf_fstab(mnt_path="/mnt"):
    log("[*] Configuring fstab")
    execute("genfstab -U {}".format(mnt_path), outfile="{}/etc/fstab".format(mnt_path))

def conf_tz(region="Australia", city="Sydney", mnt_path="/mnt"):
    log("[*] Setting timezone")
    execute("ln -sf {}/usr/share/zoneinfo/{}/{} {}/etc/localtime".format(mnt_path, region, city, mnt_path))

def conf_locale(locale="en_US.UTF-8 UTF-8", lang="LANG=en_US.UTF-8", mnt_path="/mnt"):
    log("[*] Configuring localization settings")
    write_file(locale, "{}/etc/locale.gen".format(mnt_path))
    write_file(lang, "{}/etc/locale.conf".format(mnt_path))

def conf_network(hostname="lappy", mnt_path="/mnt"):
    log("[*] Configuring network hosts")
    write_file(hostname, "{}/etc/hostname".format(mnt_path))
    hosts = "127.0.0.1\tlocalhost\n127.0.0.1\t{}".format(hostname)
    write_file(hosts, "{}/etc/hosts".format(mnt_path))

def conf_users(sudo_user="c4tdog", mnt_path="/mnt"):
    log("[*] Configuring users")
    log("[+] Set root password")
    execute("passwd root", chroot_dir=mnt_path, interactive=True)

    log("[+] Creating sudo user")
    execute("useradd -mG wheel {}".format(sudo_user), chroot_dir=mnt_path)
    execute("passwd {}".format(sudo_user), chroot_dir=mnt_path)
    sudoers = "root ALL=(ALL:ALL) ALL\n" + "%wheel ALL=(ALL:ALL) ALL\n" + "@includedir /etc/sudoers.d"
    write_file(sudoers, "{}/etc/sudoers".format(mnt_path))

def install_grub(luks_partition, luks_name="cryptlvm", mnt_path="/mnt"):
    log ("[*] Creating grub encryption key")
    key_path = "{}/root/{}.keyfile".format(mnt_path, luks_name)
    print(key_path)
    execute("dd bs=512 count=4 if=/dev/random of={} iflag=fullblock".format(key_path))
    execute("chmod 000 {}".format(key_path))
    execute("cryptsetup -v luksAddKey {} {}".format(luks_partition, key_path), interactive=True)
    
    log("[*] Configuring intram with encrypted boot")
    initram = "{}\nFILES=(/root/{}.keyfile)".format(INITRAM_HOOKS, luks_name)
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

def enable_services(services={"lightdm", "NetworkManager"}, mnt_path="/mnt"):
    log("[*] Enabling services")
    for serv in services:
        execute("systemctl enable {}".format(serv), chroot_dir=mnt_path)

# Use of arch-chroot is hacky and inconsistent :(
def install_yay(mnt_path="/mnt", sudo_user="c4tdog"):
    # makepkg must be run as non-root user and from dir of pkg being installed
    yay_dir = "/home/{}/yay".format(sudo_user)

    su = "su {}".format(sudo_user)
    clone_repo = "git clone {} {}".format(YAY_REPO, yay_dir)
    build_yay = "cd {}; makepkg -si --noconfirm".format(yay_dir)

    execute(su, stdin=clone_repo, chroot_dir=mnt_path)
    execute(su, stdin=build_yay, chroot_dir=mnt_path, interactive=True)

def configure():
    return

if __name__ == "__main__":
    main()


