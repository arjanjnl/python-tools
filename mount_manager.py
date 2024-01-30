#!/usr/bin/env python3
import os
import subprocess
from logger import Logger

class MountManager:
    def __init__(self, mount_point, mount_type=None, device=None, remote_path=None, server=None, user=None, password=None, credential_file=None, port=None, secure=None):
        # Initialize any configuration or state needed
        self.__mount_point = mount_point
        self.__mount_type = mount_type
        self.__device = device
        self.__remote_path = remote_path
        self.__server = server
        self.__user = user
        self.__password = password
        self.__credential_file = credential_file
        self.__port = port
        self.__secure = secure

        self.__logger = Logger()

    def mount(self):
        if self.__is_mounted():
            print(f"Filesystem at {self.__mount_point} is already mounted.")
            return

        try:
            if not self.__device and not self.__mount_type:
                # Check if self.__mount_point is in /etc/fstab
                if self.__is_in_fstab():
                    self.__logger.log(f'Mounting {self.__mount_point} from /etc/fstab.')
                    subprocess.run(['mount', self.__mount_point])
                else:
                    self.__logger.log(f"Device and type not specified, and {self.__mount_point} is not in /etc/fstab.")

            elif not self.__mount_type:
                # Attempt to mount the device on the mount point directly
                self.__logger.log(f'Mounting {self.__mount_point} filesystem from device: {self.__device}.')
                subprocess.run(['mount',self.__device, self.__mount_point])

            elif self.__mount_type == 'sshfs':
                # Mount an SSHFS filesystem
                if not all([self.__server, self.__user]):
                    raise ValueError('Missing required arguments for sshfs')
                sshfs_cmd = ['sshfs']
                if self.__port:
                    sshfs_cmd.extend(['-p', str(self.__port)])
                sshfs_cmd.extend([f"{self.__user}@{self.__server}:{self.__remote_path}", self.__mount_point])
                self.__logger.log(f'Mounting {self.__mount_type} share {self.__mount_point} from {self.__server}.')
                subprocess.run(sshfs_cmd)

            elif self.__mount_type == 'nfs':
                # Mount an NFS filesystem
                self.__logger.log(f'Mounting {self.__mount_type} share {self.__mount_point} from {self.__server}.')
                subprocess.run(['mount', f"{self.__server}:{self.__remote_path}", self.__mount_point])

            elif self.__mount_type == 'cifs':
                # Mount a CIFS (Samba) share
                cifs_cmd = ['mount.cifs']
                options = []
                if self.__credential_file:
                    options.append(f"credentials={self.__credential_file}")
                elif self.__user and self.__password:
                    options.append(f"user={self.__user},pass={self.__password}")
                else:
                    raise ValueError('No username/password or credential file provided.')
                if self.__secure:
                    options.append("seal")
                options_str = ','.join(options)
                cifs_cmd.extend(['-o', options_str, f"//{self.__server}{self.__remote_path}", self.__mount_point])
                self.__logger.log(f'Mounting {self.__mount_type} share {self.__mount_point} from {self.__server}.')
                subprocess.run(cifs_cmd)

            else:
                raise ValueError(f"Unsupported mount type: {self.__mount_type}")
            
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error mounting {self.__mount_point}: {e}")

    def umount(self):
        if not self.__is_mounted():
            self.__logger.log(f"Filesystem at {self.__mount_point} is not mounted.")
            return

        try:
            self.__logger.log(f'Mounting {self.__mount_point} filesystem.')
            subprocess.run(['umount', '-f', self.__mount_point])
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error unmounting {self.__mount_point}: {e}")

    def __is_mounted(self):
        return os.path.ismount(self.__mount_point)

    def __is_in_fstab(self):
        try:
            with open('/etc/fstab', 'r') as f:
                fstab_entries = f.readlines()
            
            return any(len(entry.split()) > 1 and entry.split()[1] == self.__mount_point for entry in fstab_entries)
        except Exception as e:
            # Handle any exceptions that might occur while reading the file
            self.__logger.log(f"Error reading /etc/fstab: {str(e)}")
            return False


    # Example: Mount an SSHFS filesystem
    # manager.mount("/mnt/sshfs_mount", mount_type="sshfs", server="example.com", user="username", device="/path/to/remote/folder")

    # Example: Unmount the same filesystem
    # manager.umount("/mnt/sshfs_mount")
