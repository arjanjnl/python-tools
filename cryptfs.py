#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
import argparse
import base64
import getpass
import shutil
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import logging  # Changed from 'from logger import Logger'
from mount_manager import MountManager
from systemd.journal import JournalHandler

class CryptFs:
    def __init__(self, config=None):
        self.__interactive = sys.stdin.isatty()

        if logging.getLogger().hasHandlers():
            self.__logger = logging.getLogger(__name__)

        self.__config = config or {}
        self.__remote_type = self.__config.get("remote_type")
        self.__remote_server = self.__config.get("remote_server")
        self.__remote_port = self.__config.get("remote_port", None)
        self.__remote_path = self.__config.get("remote_path")
        self.__remote_username = self.__config.get("remote_username", None)
        self.__remote_password = self.__config.get("remote_password", None)
        self.__remote_credential_file = self.__config.get("remote_credential_file", None)
        self.__remote_mount_point = Path(self.__config.get("remote_mount_point"))
        self.__remote_secure = self.__config.get("remote_secure", False)
        self.__key_file = Path(self.__config.get("key_file"))
        self.__crypt_mount_point = self.__config.get("crypt_mount_point", None)
        self.__crypt_file_name = Path(self.__config.get("crypt_file_name"))
        self.__crypt_device_name = 'crypt_backup'
        self.__crypt_device_path = '/dev/mapper'
        self.__crypt_device = os.path.join(self.__crypt_device_path, self.__crypt_device_name)
        self.__crypt_file = None

        # Initialize logger
        self.__logger = logging.getLogger("CryptFs")

    def configure_logging(self):
        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG if self.__interactive else logging.INFO)
        logger.handlers.clear()  # Clear existing handlers to avoid duplicates

        # Formatter for all handlers
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        # Add console handler for interactive mode
        if self.__interactive:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.DEBUG)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        # Add JournalHandler for logging to the systemd journal
        journal_handler = JournalHandler()
        journal_handler.setFormatter(formatter)
        logger.addHandler(journal_handler)
        self.__logger = logger

    def set_crypt_mount_point(self, mount_point):
        self.__crypt_mount_point = Path(mount_point)

    def get_crypt_mount_point(self):
        return self.__crypt_mount_point    

    def set_key_file(self, key_file):
        self.__key_file = Path(key_file)

    def set_crypt_file(self, crypt_file):  
        self.__crypt_file = Path(crypt_file)    

    def __mount_remote_fs(self):
        # Check if the input for the method is valid.
        try:
            if self.__remote_type == 'sshfs':
                mount_fs = MountManager(self.__remote_mount_point, mount_type=self.__remote_type, remote_path=self.__remote_path, server=self.__remote_server, user=self.__remote_username, port=self.__remote_port)
            elif self.__remote_type == 'cifs':
                mount_fs = MountManager(self.__remote_mount_point, mount_type=self.__remote_type, remote_path=self.__remote_path, server=self.__remote_server, credential_file=self.__remote_credential_file, secure=self.__remote_secure)
            mount_fs.mount()
        except Exception as e:
            self.__logger.error(f'Error mounting {self.__remote_mount_point}.')

    def __unlock_key(self):
        try:
            with open(self.__key_file, 'rb') as key_file:
                self.__derived_key = key_file.read()
        except Exception as e:
            self.__logger.error(f'Error reading key file: {e}')
            return

    def __prepare_open(self):
        self.__mount_remote_fs()
        self.__unlock_key()
        self.__crypt_file = Path(self.__remote_mount_point) / self.__crypt_file_name

    def generate_key(self, password):
        # Derive an encryption key from the provided password using PBKDF2
        salt = os.urandom(16)  # Generate a random salt
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            iterations=100000,  # Adjust the number of iterations as needed for your security requirements
            salt=salt,
            length=32
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))

        # Save the derived key to the key file
        with open(self.__key_file, 'wb') as key_file:
            key_file.write(key)

    def mount_remote(self):
        self.__mount_remote_fs()        

    def create_fs_file(self, size_unit):
        # Create the encrypted filesystem file
        size, unit = self.__parse_size_unit(size_unit)

        if not self.__get_valid_unit(unit):
            exit(1)

        if self.__config:
            self.__prepare_open()

        # Calculate the size in bytes based on the unit
        size_bytes = self.__convert_size_to_bytes(size, unit)
        available_space = self.__get_available_space(self.__remote_mount_point)

        # Check if there is enough free space in the filesystem
        if size_bytes > available_space:
            self.__logger.error(f'Not enough free space in the filesystem. {self.__size_of_format(available_space)} available. {size}{unit} needed.')
            exit(1)            
        try:

            subprocess.run(['dd', 'if=/dev/zero', f'of={self.__crypt_file}', f'bs=1{unit}', f'count={size}'])
            subprocess.run(['cryptsetup', 'luksFormat', '--key-file', '-', self.__crypt_file],input=self.__derived_key, check=True)
            subprocess.run(
                [
                    "cryptsetup",
                    "open",
                    "--key-file",
                    "-",
                    self.__crypt_file,
                    self.__crypt_device,
                ],
                input=self.__derived_key,
                check=True,
            )
            subprocess.run(["mkfs.xfs", self.__crypt_device])

            crypt_mount = MountManager(
                self.__crypt_mount_point, device=self.__crypt_device)
            crypt_mount.mount()

            self.__logger.info(f'Created encrypted filesystem on {self.__crypt_file}.')
        except subprocess.CalledProcessError as e:
            self.__logger.error(f'Error creating filesystem: {e}')    

    def resize_fs(self, size_unit):
        # Resize the encrypted filesystem file
        size, unit = self.__parse_size_unit(size_unit)

        if not self.__get_valid_unit(unit):
            exit(1)

        self.__prepare_open()

        size_bytes = self.__convert_size_to_bytes(size, unit)
        current_size = os.path.getsize(self.__crypt_file)
        new_size = current_size + size_bytes

        path = self.__crypt_file.parent
        formatted_size = self.__size_of_format(new_size)

        available_space = self.__get_available_space(path)

        if new_size > available_space:
            self.__logger.error(f'Not enough free space in the filesystem. {self.__size_of_format(available_space)} available. {formatted_size} needed.')
            exit(1) 

        try:
            with open(self.__crypt_file, 'ab') as fs_file:
                fs_file.truncate(new_size)
            subprocess.run(
                [
                    "cryptsetup",
                    "open",
                    "--key-file",
                    "-",
                    self.__crypt_file,
                    self.__crypt_device_name,
                ],
                input=self.__derived_key,
                check=True,
            )
            subprocess.run(
                ["cryptsetup", "resize", "--key-file", "-", self.__crypt_device],
                input=self.__derived_key,
                check=True,
            )

            crypt_mount = MountManager(self.__crypt_mount_point, device=self.__crypt_device)
            crypt_mount.mount()

            subprocess.run(['xfs_growfs', self.__crypt_mount_point])
            self.__logger.info(f'Resized {self.__crypt_mount_point} to {formatted_size}.')
        except subprocess.CalledProcessError as e:
            self.__logger.error(f'Error resizing filesystem: {e}')         

    def unlock_fs(self):
        self.__prepare_open()

        # Unlock the encrypted filesystem using the derived key
        try:
            subprocess.run(
                [
                    "cryptsetup",
                    "open",
                    "--key-file",
                    "-",
                    self.__crypt_file,
                    self.__crypt_device_name,
                ],
                input=self.__derived_key,
                check=True,
            )

            crypt_mount = MountManager(self.__crypt_mount_point, device=self.__crypt_device)
            crypt_mount.mount()
            self.__logger.info(f'Mounted encrypted file {self.__crypt_file} on {self.__crypt_mount_point}.')
        except subprocess.CalledProcessError as e:
            self.__logger.error(f'Error unlocking encrypted filesystem: {e}')

    def lock_fs(self):
        umount_crypt_fs = MountManager(self.__crypt_mount_point)
        umount_crypt_fs.umount()

        self.__logger.info(f'Unmounted encrypted filesystem {self.__crypt_mount_point}.')
        subprocess.run(["cryptsetup", "close", self.__crypt_device])

        umount_fs = MountManager(self.__remote_mount_point)
        umount_fs.umount()

    def __size_of_format(num, suffix='B'):
        for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
            if abs(num) < 1024.0:
                return f"{num:.1f} {unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f} Y{suffix}"

    def __parse_size_unit(input_str):
        try:
            # Split the input into size and unit
            size_str, unit = input_str[:-1], input_str[-1]
            # Convert size to an integer
            size = int(size_str)
            return size, unit
        except ValueError:
            raise ValueError("Invalid size/unit format. Example format: '500G'")

    def __convert_size_to_bytes(size, unit):
        units = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
        if unit in units:
            return size * units[unit]
        else:
            raise ValueError(f"Invalid unit: {unit}")

    def __get_valid_unit(self, unit):
        valid_units = ('K', 'M', 'G', 'T')
        if unit not in valid_units:
            raise ValueError(f"Invalid unit: {unit}. Valid units are {', '.join(valid_units)}")
        return True

    def __get_available_space(path, unit=None):
        try:
            # Get disk usage statistics for the specified path
            usage = shutil.disk_usage(path)

            # Calculate the available space in bytes
            available_space = usage.free

            # Convert available space to a human-readable format (e.g., GiB)
            if unit:
                units = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
                if unit in units:
                    return available_space * units[unit]

            return available_space

        except Exception as e:
            # Handle any errors that may occur
            raise ValueError(f"Error retrieving disk usage: {e}")        

def main():
    parser = argparse.ArgumentParser(description='Encrypted FS tool', formatter_class=argparse.RawTextHelpFormatter)

    # Main options
    main_group = parser.add_argument_group('Main Options')
    main_group.add_argument('-c', '--config', help='Path to YAML config file\nUse the following options with -c/--config:')
    
    # Config-related options
    config_group = parser.add_argument_group('Config Commands',
                                            'These options are used in conjunction with -c/--config')
    config_group.add_argument('-m', '--mount', action='store_true', help='Mount the remote filesystem')
    config_group.add_argument('-l', '--lock', action='store_true', help='Lock the encrypted filesystem')
    config_group.add_argument('-ul', '--unlock', action='store_true', help='Unlock the encrypted filesystem')
    config_group.add_argument('-cf', '--createfs', action='store_true', help='Create an encrypted filesystem file (use -s/--size with this option)')
    config_group.add_argument('-rs', '--resizefs', action='store_true', help='Resize an encrypted filesystem file (use -s/--size with this option)')

    # Independent options
    independent_group = parser.add_argument_group('Independent Commands',
                                                'These options can be used without -c/--config')
    independent_group.add_argument('-gk', '--genkey', action='store_true', help='Generate an encryption key (without config file)')

    # Options for createfs and resizefs
    fs_group = parser.add_argument_group('Filesystem Size Options',
                                        'Use these options with -cf/--createfs or -rs/--resizefs')
    fs_group.add_argument('-s', '--size', help='Size of filesystem')

    # Options for createfs without config
    createfs_group = parser.add_argument_group('CreateFS Options',
                                            'Use these options with -cf/--createfs without -c/--config')
    createfs_group.add_argument('-kf', '--keyfile', help='Key file')
    createfs_group.add_argument('-ef', '--encrypted-file', help='Encrypted file name')

    args = parser.parse_args()

    # Handle genkey without config file
    if args.genkey and not args.config:
        if not args.keyfile:
            print("Key file is required for genkey without a config file.")
            sys.exit(1)
        cryptfs = CryptFs()
        cryptfs.configure_logging()
        output_file = args.output_file
        cryptfs.set_key_file(output_file)
        password = getpass.getpass("Enter a password: ")
        cryptfs.generate_key(password)

    # Validate config file if specified
    if args.config:
        if not os.path.isfile(args.config):
            print(f"Config file not found: {args.config}")
            sys.exit(1)

        with open(args.config) as file:
            config = yaml.safe_load(file)
            encryptfs_config = config.get("encryptfs", {})
            cryptfs = CryptFs(
                encryptfs_config
            )  # Assuming an instance of CryptFs is needed
            cryptfs.configure_logging()

        if args.mount:
            cryptfs.mount_remote()

        elif args.unlock:
            cryptfs.unlock_fs()

        elif args.lock:
            cryptfs.lock_fs()

        elif args.createfs:
            if not args.size:
                print("Size is required for createfs.")
                sys.exit(1)
            cryptfs.create_fs_file(args.size)

        elif args.resizefs:
            if not args.size:
                print("Size is required for resizefs.")
                sys.exit(1)
            cryptfs.resize_fs(args.size)

        elif args.genkey:
            password = getpass.getpass("Enter a password: ")
            cryptfs.generate_key(password)

        else:
            # Handle other commands or show help message
            parser.print_help()

    # Handle createfs without config file
    elif args.createfs:
        if not (args.keyfile and args.encrypted_file and args.size):
            print(
                "Keyfile, encrypted file, and size are required for createfs without a config file."
            )
            sys.exit(1)

        cryptfs = CryptFs()
        cryptfs.configure_logging()
        key_file = args.key_file
        encrypted_file = args.encrypted_file
        size = args.size
        cryptfs.set_key_file(key_file)
        cryptfs.set_crypt_file(encrypted_file)
        cryptfs.create_fs_file(size)    

    else:
        # Handle other commands or show help message
        parser.print_help()              

if __name__ == "__main__":
    main()        
