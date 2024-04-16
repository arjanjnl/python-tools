#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
import shutil   
import textwrap
from datetime import datetime
import time
from pathlib import Path
import argparse
import socket
from cryptfs import CryptFs
import logging  # Changed from 'from logger import Logger'
from mount_manager import MountManager
from systemd.journal import JournalHandler
from logging.handlers import SMTPHandler

# Class for the rsync backup.
class RsyncBackup:
    def __init__(self, config_file, dryrun=False, verbose=False):
        self.__interactive = sys.stdin.isatty()
        self.__verbose = verbose
        if not logging.getLogger().hasHandlers():
            self.__logger = logging.getLogger(__name__)
        self.__rsync_backups = []

        self.__dryrun = dryrun

        self.__config_file = config_file
        with open(self.__config_file) as file:
            config = yaml.safe_load(file)

        self.__backup_user = config.get("remote_user", "root")
        self.__date_format = config.get("backup_date_format", "%Y%m%d")
        self.__backup_date = datetime.now().strftime(self.__date_format)
        self.__backup_location = config["backup_location"]
        self.__need_mount_fs = config.get("need_mount_fs", True)
        self.__number_of_versions = config.get("number_of_versions", 180)
        encrypt_storage = config.get("encrypt_storage", False)
        mail_config = config.get("mail", False)

        # Create a list of objects from the yaml file.
        source_location = config.get("source_location", {})
        for server_name, directories in source_location.items():
            directory_list = []
            for directory in directories:
                directory_path = directory.get("directory")
                if directory_path:
                    exclude_list = directory.get("exclude", [])
                    directory_list.append((Path(directory_path), exclude_list))

            self.__rsync_backups.append(
                RsyncObject(server_name, directory_list)
            )

        if "encryptfs" in config and encrypt_storage:
            cryptfs_config = config["encryptfs"]
            self.__cryptfs = CryptFs(cryptfs_config)
        else:
            self.__cryptfs = None

        if "mail" in config and mail_config:
            self.__mail = True
            self.__local_delivery_user = mail_config.get("local_delivery_user", None)
            if not self.__local_delivery_user:  
                self.__mail_server = mail_config.get("mail_server", None)
                self.__to_address = mail_config.get("to_address", None)
                self.__from_address = mail_config.get("from_address", self.__to_address)
                self.__subject = (f'Backup {self.__config_file} on {self.__backup_date}')
                mail_user = mail_config.get("mail_user", None)
                mail_password = mail_config.get("mail_password", None)
                if mail_user and mail_password:
                    self.__credentials = (mail_user, mail_password)
                else:
                    self.__credentials = None
        else:
            self.__mail = False                             

    def configure_logging(self, mail_log=True):
        configure_mail_log = mail_log
        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG if self.__verbose else logging.INFO)
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
        journal_handler.setLevel(logging.INFO)
        journal_handler.setFormatter(formatter)
        logger.addHandler(journal_handler)

        if configure_mail_log and self.__mail:
            self.__mail_handler = self.__create_mail_handler()
            if self.__mail_handler:
                self.__mail_handler.setLevel(logging.DEBUG if self.__verbose else logging.INFO)
                logger.addHandler(self.__mail_handler)

        self.__logger = logger

    def __create_mail_handler(self):
        if self.__local_delivery_user:
            mail_handler = MailHandler(local_user=self.__local_delivery_user)
        else:    
            mail_handler = MailHandler(
                mailhost=self.__mail_server,
                fromaddr=self.__from_address,
                toaddrs=[self.__to_address],
                subject=self.__subject,
                credentials=self.__credentials if self.__credentials else None,
                secure=(),
            )

        mail_handler.setLevel(logging.INFO)  # Adjust as necessary
        mail_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        return mail_handler

    # Set the start time of the backup.
    def __pre_backup(self):
        self.start_time = time.time()
        self.__logger.info(f'Start backup process with config file {self.__config_file}')
        self.__logger.info(f'Interactive is set to "{self.__interactive}" and verbose is set to "{self.__verbose}".')

    # Set the end time of the backup.
    def __post_backup(self):
        difference = (time.time() - self.start_time)
        self.__logger.info(f'Finished all backups - Total run time {difference}')          

    # Method for checking if there are older backups in the backup location.
    def __get_old_backup_dates(self, target_dir):
        # If the target directory doesn't exist return None.
        if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
            return []

        # Fill the dirs list with all the directories in the target_dir excluding
        # directoris with a name not conform the date_format and the current date.
        dirs = [
            dir_name 
            for dir_name in os.listdir(target_dir) 
            if self.__is_valid_date_format(dir_name)
            ]
        return sorted(dirs)
    
    @staticmethod
    def __get_old_backup_directories(target_location, previous_backup_dates, directory):
        directories = []
        if previous_backup_dates:
            directories = [
                previous_backup for previous_backup in previous_backup_dates
                if Path(target_location, previous_backup, directory).is_dir()
            ]
        return sorted(directories)

    # Method for checking the name given is conform the date format.
    def __is_valid_date_format(self, dir_name):
        try:
            datetime.strptime(dir_name, self.__date_format)
            return True
        except ValueError:
            return False

    # Method for the backup.
    def __rsync(self, selected_servername=None):
        for backup in self.__rsync_backups:
            servername = backup.get_servername()

            if selected_servername and servername != selected_servername:
                continue

            fqdn_name = socket.gethostname()
            short_name = fqdn_name.split('.')[0]
            system_is_local = short_name == servername or fqdn_name == servername
            
            target_location = Path(self.__backup_location) / servername  
            previous_backup_dates = self.__get_old_backup_dates(target_location)

            # Check if the target location exists. If not than create the directory.
            target_location_path = Path(target_location)
            target_location_path.mkdir(parents=True, exist_ok=True)


            for directory in backup.get_directory_list():
                self.__logger.info(directory)

                remote_location = f'{self.__backup_user}@{servername}:{directory}/'

                excludes = backup.get_excludes_for_directory(directory)

                directory_parts = directory.parts[1:] if directory.parts[0] == '/' else directory.parts
                stripped_directory = Path(*directory_parts)

                previous_backups = self.__get_old_backup_directories(target_location, previous_backup_dates, stripped_directory)
                # Remove the current backup date if it exists
                previous_backups = [date for date in previous_backups if date != self.__backup_date]
                # Determine the last backup based on the sorted list
                last_backup = sorted(previous_backups)[-1] if previous_backups else None
            
                # Check if the directory exists
                object_target = Path(target_location) / self.__backup_date / stripped_directory
                if object_target.exists():
                    self.__logger.info(f'Backup target {object_target} already exists.')
                    continue
                else:
                    # Create the directory if it doesn't exist
                    object_target.mkdir(parents=True)

                # First part of the rsync command.
                rsync_cmd = ["rsync", "-arv"]

                if self.__dryrun:
                    rsync_cmd.extend(["--dry-run"])

                # Add the source and destination paths

                source_path = f'{directory}/' if system_is_local else remote_location
                rsync_cmd.extend([source_path, object_target])

                # If there are excludes than add them to the rsync_cmd line.
                if excludes:
                    for pattern in excludes:
                        rsync_cmd.extend(["--exclude", pattern])

                # If there is a previous backup than use it as an link destination in the rsync_cmd.
                if last_backup is not None:
                    rsync_cmd.extend(["--link-dest", f'{target_location}/{last_backup}{directory}'])

                self.__logger.info(f'Running command: {rsync_cmd}.')
                result = subprocess.run(rsync_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if self.__verbose or self.__interactive:  # Check if we should log the subprocess output
                    self.__logger.debug(result.stdout)
                    if result.stderr:
                        self.__logger.error(result.stderr)


            # If there are older backups that have to be removed than remove it.
            if len(previous_backup_dates) > self.__number_of_versions:
                oldest_backup = previous_backup_dates[0]
                oldest_backup_path = Path(target_location, oldest_backup)
                if oldest_backup_path.is_dir():  # Check if the path is indeed a directory
                    shutil.rmtree(oldest_backup_path)
                    self.__logger.info(f'Removing backup directory for {servername} with date {oldest_backup}.')

    @staticmethod
    def __human_readable_size(size, decimal_places=2):
        for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']:
            if size < 1024.0:
                break
            size /= 1024.0
        return f"{size:.{decimal_places}f} {unit}"

    def __check_number_backups(self, check_type=None, selected_servername=None):
        total, used, free = shutil.disk_usage(self.__backup_location)
        print(f"Filesystem total size: {self.__human_readable_size(total)}, Used size: {self.__human_readable_size(used)}, Free size: {self.__human_readable_size(free)}")

        server_backup_counts = {}

        for backup in self.__rsync_backups:
            servername = backup.get_servername()
            if selected_servername and servername != selected_servername:
                continue

            target_location = Path(self.__backup_location) / servername
            previous_backup_dates = self.__get_old_backup_dates(target_location)

            for directory in backup.get_directory_list():
                directory_parts = directory.parts[1:] if directory.parts[0] == '/' else directory.parts
                stripped_directory = Path(*directory_parts)
                
                previous_backups = self.__get_old_backup_directories(target_location, previous_backup_dates, stripped_directory)

                if servername not in server_backup_counts:
                    server_backup_counts[servername] = []
                server_backup_counts[servername].append((stripped_directory, len(previous_backups), previous_backups))

        for servername, backups in server_backup_counts.items():
            print(f"\033[1mServer:\033[0m \033[1;93m{servername}\033[0m")
            print(f"\033[1m{'Directories':<20} Nr backups\033[0m")
            for object_dir, count, directories in backups:
                if check_type == 'full':
                    backup_names = ', '.join(sorted(directories)) 
                elif check_type == 'last':
                    backup_names = sorted(directories)[-1]    
                elif check_type == 'default':
                    backup_names = ''
                
                # Initial part with object_dir and count
                initial_part = f"- {str(object_dir):<18} \033[93m{count}\033[0m"
                # Determine where backup names should start, considering a space after count
                tab_width = 8
                start_position = len(initial_part)
                
                # Use textwrap to wrap the backup_names, starting from the calculated start_position
                wrapped_lines = textwrap.wrap(backup_names, width=shutil.get_terminal_size().columns - start_position, initial_indent=' ' * tab_width, subsequent_indent=' ' * start_position)

                # Print the initial part and the first line of backup names (if available) on the same line
                if wrapped_lines:
                    print(initial_part + " " + wrapped_lines[0])
                    # Print any additional lines of backup names, if present
                    for line in wrapped_lines[1:]:
                        print(line)
                else:
                    # If there are no backup names, just print the initial part
                    print(initial_part)

    # Method that runs the mount, rsync and umount methods.
    def backup(self, selected_servername=None):
        mount_fs = MountManager(self.__backup_location)

        self.__pre_backup()

        if self.__cryptfs:
            self.__cryptfs.set_crypt_mount_point(self.__backup_location)
            self.__cryptfs.unlock_fs()

        if self.__need_mount_fs:
            mount_fs.mount()

        self.__rsync(selected_servername)

        if self.__need_mount_fs:
            mount_fs.umount()

        if self.__cryptfs:
            self.__cryptfs.lock_fs()

        self.__post_backup()

    def check_backups(self, check_type=None, selected_servername=None):
        mount_fs = MountManager(self.__backup_location)

        if self.__cryptfs:
            self.__cryptfs.set_crypt_mount_point(self.__backup_location)
            self.__cryptfs.unlock_fs()

        if self.__need_mount_fs:
            mount_fs.mount()

        self.__check_number_backups(check_type, selected_servername)

        if self.__need_mount_fs:
            mount_fs.umount()

        if self.__cryptfs:
            self.__cryptfs.lock_fs()
    
    def mount(self, mount_type):
        mount_fs = MountManager(self.__backup_location)

        if mount_type == 'mount':
            if self.__cryptfs:
                self.__cryptfs.set_crypt_mount_point(self.__backup_location)
                self.__cryptfs.unlock_fs()

            if self.__need_mount_fs:
                mount_fs.mount()

        elif mount_type == 'umount':
            if self.__need_mount_fs:
                mount_fs.umount()

            if self.__cryptfs:
                self.__cryptfs.lock_fs()            

# Class for each backup object.
class RsyncObject:
    def __init__(self, servername, directory_list):
        self.__servername = servername
        self.__directory_list = directory_list

    # Method for returng the servername.
    def get_servername(self):
        return self.__servername

    # Return the object name.
    def get_directory_list(self):
        return [Path(directory) for directory, _ in self.__directory_list]


    # Get the exclude list.
    def get_excludes_for_directory(self, directory):
        # Find the tuple with the matching directory and return its exclude list
        for dir_path, exclude_list in self.__directory_list:
            if dir_path == directory:
                return exclude_list
        return []
    
class MailHandler(logging.Handler):
    def __init__(self, mailhost=None, fromaddr=None, toaddrs=None, subject=None, credentials=None, secure=None, local_user=None):
        super().__init__()
        self.__buffer = []  # To store log records
        self.__local_user = local_user
        self.__mailhost = mailhost
        self.__fromaddr = fromaddr
        self.__toaddrs = toaddrs
        self.__subject = subject
        self.__credentials = credentials
        self.__secure = secure if secure else ()
        
        self.__mode = "local" if local_user else "smtp"

    def emit(self, record):
        # Add the formatted log message to the buffer
        self.__buffer.append(self.format(record))

    def flush(self):
        # Check the mode to determine how to send the buffered messages
        if self.__mode == "local":
            self.__send_local("\n".join(self.__buffer))
        else:  # SMTP mode
            self.__send_smtp(self.__buffer)
        self.__buffer.clear()  # Clear the buffer after sending
        super().flush()

    def __send_local(self, msg):
        try:
            sendmail = subprocess.Popen(["/usr/sbin/sendmail", self.__local_user], stdin=subprocess.PIPE)
            sendmail.communicate(msg.encode('utf-8'))
        except Exception as e:
            self.handleError(e)

    def __send_smtp(self, msgs):
        if not self.__mailhost or not self.__fromaddr or not self.__toaddrs:
            print("SMTP configuration is incomplete.")
            print(self.__mailhost, self.__fromaddr, self.__toaddrs)
            return
        
        full_msg = "\n".join(msgs)
        try:
            smtp_handler = SMTPHandler(self.__mailhost, self.__fromaddr, self.__toaddrs, self.__subject, self.__credentials, self.__secure)
            record = logging.makeLogRecord({"msg": full_msg, "levelname": "INFO", "name": self.name})
            smtp_handler.emit(record)
        except Exception as e:
            print(f"Failed to send email: {e}")

def main():
    parser = argparse.ArgumentParser(description='Rsync Backup Script', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('-c', '--config', required=True, help='Path to YAML config file\nUse the following options with -c/--config:')
    parser.add_argument('-s', '--server', help='Specify a single servername to only backup or check.')
    parser.add_argument('-x', '--check', nargs='?', const='default', default=None, choices=['default', 'full', 'last'],
                    help='Perform a backup check. Types: default (none), full, last. Example: --check full\n'
                        '  default or leave empty: shows the number of backups for a directory.\n'
                        '  full: shows all the backups there are for a directory.\n'
                        '  last: shows the last backup there is for a directory.')
    parser.add_argument('-m', '--mount', nargs='?', choices=['mount', 'umount'], help='Only mount or umount the target filesystem for the backups.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')

    parser.add_argument('--dryrun', action='store_true', help='Simulate rsync without making any changes')
    args = parser.parse_args()

    config_file = args.config
    if config_file and not os.path.isfile(config_file):
        print(f"Config file not found: {config_file}")
        sys.exit(1)

    rsync_backup = RsyncBackup(config_file, dryrun=args.dryrun, verbose=args.verbose)

    if args.check:
        rsync_backup.configure_logging(mail_log=False)
        rsync_backup.check_backups(args.check, args.server)
    elif args.mount:
        rsync_backup.configure_logging(mail_log=False)
        rsync_backup.mount(args.mount)
    else:
        rsync_backup.configure_logging()   
        rsync_backup.backup(args.server)

if __name__ == "__main__":
    main()
