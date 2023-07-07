########################################
#                                      #
#                                      #
#      install_util.py - Tony Ward     #
#                                      #
#  Basic utilities used in install.py  #
#                                      #
#                                      #
########################################

from urllib import request
from configparser import ConfigParser
import subprocess
import re
import os

INSTALL_SETTINGS = {"luks_name", "tz.region", "tz.city", "hostname", "mount_path", "sudo_user"}

# Chroot string for execute
CHROOT = "arch-chroot"

# Escape characters to format text color in log
FG_WHITE = '\u001b[37m'
FG_RED   = '\u001b[31m'

def has_network(timeout=5):
    log("[*] Checking internet connection")
    try:
        request.urlopen('https://google.com', timeout=timeout)
        log("[+] Internet connection good!")
        return True
    except:
        log("[!] No internet!")
        log("Connect to internet using: iwctl station <wlan> connect <SSID>")
        return False

def parse_config(config_path):
    if not os.path.isfile(config_path):
        log("[!] No config file found - {}".format(config_path))
        exit()

    config_file = ConfigParser(allow_no_value=True)
    config_file.read(config_path)
    sections = config_file.sections() 
    config_dict = {}

    if not "Install.Config" in sections:
        log("[!] No installation config in {}".format(config_path))
        exit()

    for setting in INSTALL_SETTINGS:
        if setting not in config_file["Install.Config"]:
            log("[!] Missing install setting {}".format(setting))
            exit()
        config_dict[setting] = config_file["Install.Config"][setting]
    
    if not "Pacman.Pkgs" in sections:
        log("[!] No installation packages in {}".format(config_path))
        exit()
    config_dict["pacman_pkgs"] = " ".join([pkg for pkg in config_file["Pacman.Pkgs"]])

    # TODO add yay package parsing

    return config_dict

def execute(cmd, stdin="", outfile="", chroot_dir="", interactive=False):
    if outfile != "" and interactive:
        log("[!] Cannot execute in interactive mode and save output to file")

    if chroot_dir != "":
        cmd = "{} {} {}".format(CHROOT, chroot_dir, cmd)
    cmd = cmd.split(' ')

    args = {"encoding": "utf-8"}
    if stdin != "":
        args["input"] = stdin
    if not interactive:
        args["stdout"] = subprocess.PIPE    # in non-interactive mode output can be processed

    proc = subprocess.run(cmd, **args)

    if outfile != "":
        write_file(proc.stdout, outfile)

    return proc

def write_file(string, file_path):
    file = open(file_path, "w")
    file.write(string)
    file.close()

def replace_in_file(match_line, string, file_path):
    file = open(file_path, "r")
    lines = file.readlines()
    file.close()
    file = open(file_path, "w")
    for line in lines:
        if re.match(match_line, line):
            line = string
        file.write(line)
    file.close()

def log(string):
    print(FG_RED + string + FG_WHITE)

