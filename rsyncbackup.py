#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
from datetime import datetime
import time
from pathlib import Path
import argparse
import socket
from cryptfs import CryptFs
from logger import Logger
from mount_manager import MountManager

# Set the self.__logger's name to your program's name
#logger = logging.logger(__name__)

# Class for the rsync backup.
class RsyncBackup:
    def __init__(self, config_file, dryrun=False):
        self.__interactive = sys.stdin.isatty()
        self.__rsync_backups = []

        self.__dryrun = dryrun

        with open(config_file) as file:
            config = yaml.safe_load(file)

        self.__backup_user = config.get('remote_user', 'root')
        self.__date_format = config.get('backup_date_format', "%Y%m%d")
        self.__backup_date = datetime.now().strftime(self.__date_format)
        self.__backup_location = config['backup_location']
        self.__need_mount_fs = config.get('need_mount_fs', True)
        self.__number_of_versions = config.get('number_of_versions', 180)
        encrypt_storage = config.get('encrypt_storage', False)

        self.__logger = Logger()
        
        # Create a list of objectes from the yaml file.
        source_location = config.get("source_location", {})
        for server_name, directories in source_location.items():
            for directory_config in directories:
                directory = directory_config.get('directory', '')
                exclude_list = directory_config.get('exclude', [])  # Get exclude list if specified
                self.__rsync_backups.append(RsyncObject(server_name, directory, exclude_list))

        if "encryptfs" in config and encrypt_storage:
            cryptfs_config = config["encryptfs"]
            self.__cryptfs = CryptFs(cryptfs_config)
        else:
            self.__cryptfs = None

    # Set the start time of the backup.
    def __pre_backup(self):
        self.start_time = time.time()
        self.__logger.log("Start backup")

    # Set the end time of the backup.
    def __post_backup(self):
        difference = (time.time() - self.start_time)
        self.__logger.log(f'Finished all backups - Total run time {difference}')          

    # Method for checking if there are older backups in the backup location.
    def __get_old_backup(self, target_dir, version):
        # If the target directory doesn't exist return None.
        if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
            return None
        
        # Fill the dirs list with all the directories in the target_dir excluding 
        # directoris with a name not conform the date_format and the current date.
        dirs = [
            dir_name 
            for dir_name in os.listdir(target_dir) 
            if self.__is_valid_date_format(dir_name) and dir_name != self.__backup_date
            ]
        
        # If there are no directories in the location return None.
        if not dirs:
            return None
        
        # If the input was latest than take the last entry from the list.
        if version == "latest":
            list_address = -1
        # If the input was oldest and there are more than 180 older backups than return 
        # the first entry from the list.
        elif version == "oldest" and len(dirs) > self.__number_of_versions:
            list_address = 0
        else:
            return None

        return sorted(dirs)[list_address]

    # Method for checking the name given is conform the date format.
    def __is_valid_date_format(self, dir_name):
        try:
            datetime.strptime(dir_name, self.__date_format)
            return True
        except ValueError:
            return False
        
    # Method for the backup.    
    def __rsync(self):
        for backup in self.__rsync_backups:
            servername = backup.get_servername()
            remote_location = f'{self.__backup_user}@{servername}:{backup.get_object_name(True)}/'
            target_location = Path(self.__backup_location) / backup.get_servername()

            target_location_path = Path(target_location)
            # Check if the target location exists. If not than create the directory.
            target_location_path.mkdir(parents=True, exist_ok=True)

            object_target = Path(target_location) / self.__backup_date / backup.get_object_name(False)
            # Check if the directory exists
            if object_target.exists():
                self.__logger.log(f'Backup target {object_target} already exists.')
                continue
            else:
                # Create the directory if it doesn't exist
                object_target.mkdir(parents=True)

            # First part of the rsync command.
            rsync_cmd = ["rsync", "-arv" if self.__interactive else "-ar"]

            if self.__dryrun:
                rsync_cmd.extend(["--dry-run"])
            
            # Add the source and destination paths
            source_path = backup.get_object_name(True)
            if socket.gethostname() != servername:
                source_path = remote_location
            rsync_cmd.extend([source_path, object_target])

            # If there are excludes than add them to the rsync_cmd line.
            if backup.get_exclude():
                for pattern in backup.get_exclude():
                    rsync_cmd.extend(["--exclude", pattern])

            # If there is a previous backup than use it as an link destination in the rsync_cmd.
            previous_backup = self.__get_old_backup(target_location, "latest")
            if previous_backup is not None:
                rsync_cmd.extend(["--link-dest", f'{target_location}/{previous_backup}{backup.get_object_name(True)}'])

            self.__logger.log(f'Running command: {rsync_cmd}.')
            subprocess.run(rsync_cmd)
            
            # If there are older backups that have to be removed than remove it.
            oldest_backup = self.__get_old_backup(target_location, "oldest")
            if oldest_backup is not None:
                os.remove(Path(target_location, oldest_backup))
                self.__logger.log(f'Removing backup for {servername} with date {oldest_backup}.')

    # Method that runs the mount, rsync and umount methods.
    def backup(self):
        mount_fs = MountManager(self.__backup_location)

        self.__pre_backup()

        if self.__cryptfs:
            self.__cryptfs.set_crypt_mount_point(self.__backup_location)
            self.__cryptfs.unlock_fs()

        if self.__need_mount_fs:
            mount_fs.mount()

        self.__rsync()

        if self.__need_mount_fs:
            mount_fs.umount()

        if self.__cryptfs:
            self.__cryptfs.lock_fs()

        self.__post_backup()

# Class for each backup object.
class RsyncObject:
    def __init__(self, servername, object_name, exclude=None):
        self.__servername = servername
        self.__object_name = Path(object_name)
        self.__exclude = exclude

    # Method for returng the servername.
    def get_servername(self):
        return self.__servername

    # Return the object name.
    def get_object_name(self, slash):
        if slash:
            return self.__object_name
        if not slash:
            return self.__object_name.relative_to('/')

    # Get the exclude list.
    def get_exclude(self):
        return self.__exclude

def main():
    parser = argparse.ArgumentParser(description='Rsync Backup Script')
    parser.add_argument('config_file', help='Path to the configuration file')
    parser.add_argument('--dryrun', action='store_true', help='Simulate rsync without making any changes')
    args = parser.parse_args()

    config_file = args.config_file
    if not os.path.isfile(config_file):
        print(f"Config file not found: {config_file}")
        sys.exit(1)

    dryrun = args.dryrun
    if dryrun:
        rsync_backup = RsyncBackup(config_file, dryrun)
    else:
        rsync_backup = RsyncBackup(config_file)    
    rsync_backup.backup()

if __name__ == "__main__":
    main()
