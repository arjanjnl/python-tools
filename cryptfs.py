#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
import argparse
import base64
import getpass
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from logger import Logger
from support_methods import size_of_format, convert_size_to_bytes, parse_size_unit, get_available_space
from mount_manager import MountManager

class CryptFs:
    def __init__(self, config=None):
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
        self.__crypt_file = None

        self.__logger = Logger()

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
            self.__logger.log_error(f'Error mounting {self.__remote_mount_point}.')

    def __unlock_key(self):
        try:
            with open(self.__key_file, 'rb') as key_file:
                self.__derived_key = key_file.read()
        except Exception as e:
            self.__logger.log_error(f'Error reading key file: {e}')
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
        size, unit = parse_size_unit(size_unit)

        valid_units = ('K', 'M', 'G', 'T')
        if unit not in valid_units:
            self.__logger.log_error(f"Invalid unit: {unit}. Valid units are {', '.join(valid_units)}")
            exit(1)

        if self.__config:
            self.__prepare_open()

        # Calculate the size in bytes based on the unit
        size_bytes = convert_size_to_bytes(size, unit)
        available_space = get_available_space(self.__remote_mount_point)

        # Check if there is enough free space in the filesystem
        if size_bytes > available_space:
            self.__logger.log_error(f'Not enough free space in the filesystem. {size_of_format(available_space)} available. {size}{unit} needed.')
            exit(1)            
        try:
            crypt_device = os.path.join('/dev/mapper', self.__crypt_device_name)

            subprocess.run(['dd', 'if=/dev/zero', f'of={self.__crypt_file}', f'bs=1{unit}', f'count={size}'])
            subprocess.run(['cryptsetup', 'luksFormat', '--key-file', '-', self.__crypt_file],input=self.__derived_key, check=True)
            subprocess.run(['cryptsetup', 'open', '--key-file', '-', self.__crypt_file, self.__crypt_device_name],input=self.__derived_key, check=True)
            subprocess.run(['mkfs.xfs', crypt_device])

            crypt_mount = MountManager(self.__crypt_mount_point, device=crypt_device)
            crypt_mount.mount()

            self.__logger.log(f'Created encrypted filesystem on {self.__crypt_file}.')
        except subprocess.CalledProcessError as e:
            self.__logger.log_error(f'Error creating filesystem: {e}')    

    def resize_fs(self, size_unit):
        # Resize the encrypted filesystem file
        size, unit = parse_size_unit(size_unit)

        valid_units = ('K', 'M', 'G', 'T')
        if unit not in valid_units:
            self.__logger.log_error(f"Invalid unit: {unit}. Valid units are {', '.join(valid_units)}")
            exit(1)

        self.__prepare_open()
        
        size_bytes = convert_size_to_bytes(size, unit)
        current_size = os.path.getsize(self.__crypt_file)
        new_size = current_size + size_bytes

        path = self.__crypt_file.parent
        formatted_size = size_of_format(new_size)
        
        available_space = get_available_space(path)

        if new_size > available_space:
            self.__logger.log_error(f'Not enough free space in the filesystem. {size_of_format(available_space)} available. {formatted_size} needed.')
            exit(1) 

        try:
            with open(self.__crypt_file, 'ab') as fs_file:
                fs_file.truncate(new_size)
            subprocess.run(['cryptsetup', 'open', '--key-file', '-', self.__crypt_file, self.__crypt_device_name],input=self.__derived_key, check=True)
            subprocess.run(['cryptsetup', 'resize', '--key-file', '-', self.__crypt_device_name],input=self.__derived_key, check=True)

            crypt_device = os.path.join('/dev/mapper', self.__crypt_device_name)
            crypt_mount = MountManager(self.__crypt_mount_point, device=crypt_device)
            crypt_mount.mount()

            subprocess.run(['xfs_growfs', self.__crypt_mount_point])
            self.__logger.log(f'Resized {self.__crypt_mount_point} to {formatted_size}.')
        except subprocess.CalledProcessError as e:
            self.__logger.log_error(f'Error resizing filesystem: {e}')         

    def unlock_fs(self):
        self.__prepare_open()

        # Unlock the encrypted filesystem using the derived key
        try:
            subprocess.run(['cryptsetup', 'open', '--key-file', '-', self.__crypt_file, self.__crypt_device_name],input=self.__derived_key, check=True)
            subprocess.run(['mount', '/dev/mapper/' + self.__crypt_device_name, self.__crypt_mount_point], check=True)
            self.__logger.log(f'Mounted encrypted file {self.__crypt_file} on {self.__crypt_mount_point}.')
        except subprocess.CalledProcessError as e:
            self.__logger.log_error(f'Error unlocking encrypted filesystem: {e}')

    def lock_fs(self):
        umount_crypt_fs = MountManager(self.__crypt_mount_point)
        umount_crypt_fs.umount()

        self.__logger.log(f'Unmounted encrypted filesystem {self.__crypt_mount_point}.')
        subprocess.run(['cryptsetup', 'close', self.__crypt_device_name])

        umount_fs = MountManager(self.__remote_mount_point)
        umount_fs.umount()

def main():
    parser = argparse.ArgumentParser(description='Encrypted FS tool')

    # Create a subparser for the top-level commands: genkey, config, createfs
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Subcommand: genkey
    genkey_parser = subparsers.add_parser('genkey', help='Generate an encryption key')
    genkey_parser.add_argument('output_file', help='Output file for the generated key')

    # Subcommand: createfs
    createfs_parser = subparsers.add_parser('createfs', help='Create a encrypted filesystem file')
    createfs_parser.add_argument('key_file', help='File containing the encryption key') 
    createfs_parser.add_argument('encrypted_file', help='Path and filename for the encrypted filesystem file')
    createfs_parser.add_argument('size_unit', help='Size of filesystem')

    # Subcommand: config
    config_parser = subparsers.add_parser('config', help='Add a configfile')
    config_parser.add_argument('yaml_file', help='YAML-filename')

    # Create a subparser for the 'config' subcommand
    config_subparsers = config_parser.add_subparsers(dest='config_command', help='Config subcommands')

    # Subcommand: config genkey
    config_genkey_parser = config_subparsers.add_parser('genkey', help='Generate an encryption key')

    # Subcommand: config genkey
    config_mount_parser = config_subparsers.add_parser('mount', help='Mount the remote filesystem')

    # Subcommand: config genkey
    config_unlock_parser = config_subparsers.add_parser('unlock', help='Unlock the encrypted filesystem')

    # Subcommand: config genkey
    config_lock_parser = config_subparsers.add_parser('lock', help='Lock the encrypted filesystem')

    # Subcommand: config createfs
    config_createfs_parser = config_subparsers.add_parser('createfs', help='Create a encrypted filesystem file')
    config_createfs_parser.add_argument('size_unit', help='Size of filesystem')

    # Subcommand: config createfs
    config_resizefs_parser = config_subparsers.add_parser('resizefs', help='Resize a encrypted filesystem file')
    config_resizefs_parser.add_argument('size_unit', help='Size of filesystem')

    args = parser.parse_args()  

    if args.command == 'config':
        yaml_file = args.yaml_file
        if not os.path.isfile(yaml_file):
            print(f"Config file not found: {yaml_file}")
            sys.exit(1)

        with open(yaml_file) as file:
            config = yaml.safe_load(file)
            encryptfs_config = config.get("encryptfs", {})  # Get the 'encryptfs' section from the config
            cryptfs = CryptFs(encryptfs_config)

            if not cryptfs.get_crypt_mount_point():
                crypt_mount_point = config.get('backup_location', None)
                if not crypt_mount_point:
                    print('No mount point given for the encrypted file.')
                    exit(1)
                cryptfs.set_crypt_mount_point(crypt_mount_point)

        # Handle subcommands within 'config' command
        if args.config_command == 'createfs':
            size_unit = args.size_unit
            cryptfs.create_fs_file(size_unit)

        elif args.config_command == 'genkey':
            password = getpass.getpass("Enter a password: ")
            cryptfs.generate_key(password)

        elif args.config_command == 'mount':
            cryptfs.mount_remote()

        elif args.config_command == 'unlock':
            cryptfs.unlock_fs()
            
        elif args.config_command == 'lock':
            cryptfs.lock_fs()

        elif args.config_command == 'resizefs':
            size_unit = args.size_unit
            cryptfs.resize_fs(size_unit)    

        else:
            # Handle other commands or show help message
            parser.print_help()              
            
    elif args.command == 'genkey':
        # Handle 'genkey' subcommand
        cryptfs = CryptFs()
        output_file = args.output_file
        cryptfs.set_key_file(output_file)
        password = getpass.getpass("Enter a password: ")
        cryptfs.generate_key(password)
        
    elif args.command == 'createfs':
        cryptfs = CryptFs()
        key_file = args.key_file
        encrypted_file = args.encrypted_file
        size_unit = args.size_unit
        cryptfs.set_key_file(key_file)
        cryptfs.set_crypt_file(encrypted_file)
        cryptfs.create_fs_file(size_unit)

    else:
        # Handle other commands or show help message
        parser.print_help()

if __name__ == "__main__":
    main()        
