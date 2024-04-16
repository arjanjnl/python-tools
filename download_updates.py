#!/usr/bin/env python3
import argparse
import os
import subprocess
import yaml
import sys
from datetime import datetime
import logging  # Changed from 'from logger import Logger'
from systemd.journal import JournalHandler
from logging.handlers import SMTPHandler

class PackageDownloader:
    def __init__(self, config_file, dryrun=False, verbose=False):
        self.__dryrun = dryrun
        self.__interactive = sys.stdin.isatty()
        self.__verbose = verbose

        if not logging.getLogger().hasHandlers():
            self.__logger = logging.getLogger(__name__)

        self.__locations = []

        source_path = None

        # Open the file and read the config
        self.__config_file = config_file
        with open(self.__config_file, "r") as yaml_file:
            config = yaml.safe_load(yaml_file)

        mail_config = config.get("mail", False)    
            
        source_location = config.get("source_location")
        protocol = config.get("protocol")
        distributions = config.get("distributions")

        self.__destination_location = config.get("destination_location")

        # For every distribution.
        for distribution, dist_data in distributions.items():
            
            # Override the source location if this differs from the global source.
            distribution_override = dist_data.get("override", False)
            if distribution_override:
                source_location = dist_data.get("source_location", source_location)
                protocol = dist_data.get("protocol", protocol)
                source_path = dist_data.get("source_path", None)
            versions = dist_data.get("versions")

            # For every version.
            for version, locations in versions.items():
                # For all the locations of a version
                for location_dict in locations:
                    location_str = list(location_dict.values())[0]

                    source_distribution = distribution
                    destination_distribution = distribution

                    alt_destination = None

                    # Add overrides:
                    location_override = location_dict.get("override")
                    if location_override:
                        source_location = location_dict.get("source_location", source_location)
                        source_path = location_dict.get("source_path", None)
                        source_distribution = location_dict.get("source_distribution", source_distribution)
                        protocol = location_dict.get("protocol", protocol)
                        alt_destination = location_dict.get("alt_destination", None)
                        destination_distribution = location_dict.get("destination_distribution", destination_distribution)

                    source_url = f"{source_location}/{source_distribution}/{location_str}"
                    destination_path = os.path.join(self.__destination_location, destination_distribution, location_str)

                    # Add a directory in the source path if source_path is set.
                    if source_path:
                        source_url = f"{source_location}/{source_path}/{source_distribution}/{location_str}"

                    # Override the destination directory of this differs from the source directory.
                    if alt_destination: 
                        destination_path = destination_path = os.path.join(self.__destination_location, destination_distribution, alt_destination)
                    
                    # Create a Location object with the source and destination and add the object to the list.
                    self.__locations.append(Locations(protocol, source_url, destination_path))

        self.__date_format = config.get("download_date_format", "%Y%m%d")
        self.__download_date = datetime.now().strftime(self.__date_format)            

        if "mail" in config and mail_config:
            self.__mail = True
            self.__local_delivery_user = mail_config.get("local_delivery_user", None)
            if not self.__local_delivery_user:  
                self.__mail_server = mail_config.get("mail_server", None)
                self.__to_address = mail_config.get("to_address", None)
                self.__from_address = mail_config.get("from_address", self.__to_address)
                self.__subject = (f'Download {self.__config_file} on {self.__download_date}')
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

    def download_updates(self):
        try:
            service_name = []
            service_name.append("apache2")

            chown_user = "wwwrun"
            chown_group = "www"

            for location in self.__locations:
                protocol = location.get_protocol()
                source_url = location.get_source_url()
                destination_path = location.get_destination_path()

                # Create the destination path if it doesn't exist.
                if not os.path.exists(destination_path):
                    os.makedirs(destination_path)

                if protocol == "rsync":
                    self.__rsync_download(source_url, destination_path)
                elif protocol == "curl":
                    self.__curl_download(source_url, destination_path)

            # Change the owner for the files so Apache can read them.
            chown_cmd = ["chown", "-R", f'{chown_user}:{chown_group}', self.__destination_location]
            if self.__verbose:  # Check if we should log the subprocess output
                result = subprocess.run(chown_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.__logger.debug(result.stdout)
                if result.stderr:
                    self.__logger.error(result.stderr)
            else:
                subprocess.run(chown_cmd, check=True)

            for service in service_name:
                self.__restart_systemd_service(service)

        except Exception as e:
            self.__logger.log_error(f"Error downloading or chowning {destination_path}: {str(e)}")

    def __restart_systemd_service(self, service_name):
        try:
            # Use the systemctl command to restart the service
            subprocess.run(["systemctl", "restart", service_name], check=True)
            self.__logger.info(f"Service {service_name} restarted successfully.")
        except subprocess.CalledProcessError as e:
            self.__logger.error(f"Error restarting service {service_name}: {e}")

    def __rsync_download(self, source_url, destination_path):
        try:
            # If the programm is called from the command line add verbose to rsync.
            rsync_command = ["rsync", "-arPv"]

            # If the dryrun option is given add --dry-run to the rsync-command
            if self.__dryrun:
                rsync_command.extend(["--dry-run"])
            
            # Delete files that are not on the source location.
            rsync_command.extend(["--delete"])    

            # Add the source and destination to the rsync commando.
            rsync_command.extend(['rsync://' + source_url + '/', destination_path + '/'])

            # Run the rsync command as a subprocces.
            result = subprocess.run(rsync_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if self.__verbose or self.__interactive:
                self.__logger.debug(result.stdout)
                if result.stderr:
                    self.__logger.error(result.stderr)

            # Log action.
            self.__logger.info(f"Downloaded: {destination_path}")
        except Exception as e:
            self.__logger.error(f"Error downloading {destination_path}: {str(e)}")

    def __curl_download(self, source_url, destination_path):
        try:
            curl_command = ["curl", "--create-dirs", "-o", destination_path, source_url]
            
            result = subprocess.run(curl_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if self.__verbose or self.__interactive:  # Check if we should log the subprocess output
                self.__logger.debug(result.stdout)
                if result.stderr:
                    self.__logger.error(result.stderr)

        except Exception as e:
            self.__logger.error(f"Error downloading {destination_path}: {str(e)}")

class Locations:
    def __init__(self, protocol, source_url, destination_path, repomd=False):
        self.__protocol = protocol
        self.__source_url = source_url
        self.__destination_path = destination_path
        self.__repomd = repomd

    def get_protocol(self):
        return self.__protocol

    def get_source_url(self):
        return self.__source_url
    
    def get_destination_path(self):
        return self.__destination_path
    
    def get_repomd(self):
        return self.__repomd
    
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
    parser = argparse.ArgumentParser(description='Rsync Backup Script')
    parser.add_argument(
        "-c",
        "--config",
        help="Path to YAML config file\nUse the following options with -c/--config:",
    )
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    parser.add_argument('--dryrun', action='store_true', help='Simulate rsync without making any changes')
    args = parser.parse_args()

    config_file = args.config
    if not os.path.isfile(config_file):
        print(f"Config file not found: {config_file}")
        sys.exit(1)


    downloader = PackageDownloader(config_file, verbose=args.verbose, dryrun=args.dryrun)
    downloader.configure_logging()
    downloader.download_updates()    

if __name__ == "__main__":
    main()
