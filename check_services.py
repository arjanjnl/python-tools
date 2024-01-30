#!/usr/bin/env python3
"""
Script to check the status of services on multiple hosts using SSH.
"""

import argparse
import os
import sys
import yaml
import paramiko


class CheckServices:
    def __init__(
        self,
        config_file,
        user=None,
        password=None,
        dryrun=False,
        short=False,
        error=False,
        short_error=False,
        lines=None,
        hostname=None,
        custom_only=False,
        no_custom=False,
        default_user="root",
    ):
        self.__dryrun = dryrun
        self.__password = password
        self.__ssh_client = paramiko.SSHClient()
        self.__ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            with open(config_file, "r") as yaml_file:
                config = yaml.safe_load(yaml_file)
        except Exception as e:
            print(f"Error reading config file: {e}", file=sys.stderr)
            sys.exit(1)

        self.__user = user if user is not None else config.get("user", default_user)
        if self.__user == "root":
            self.__sudo = False
        else:
            self.__sudo = True

        self.__lines = lines if lines is not None else 5

        self.__generic_services_list = config.get("generic_services", {}).get(
            "services", []
        )
        self.__hosts_list = [
            Hosts(
                hostname,
                self.__generic_services_list + host_config.get("services", []),
                host_config.get("custom", []),
            )
            for hostname, host_config in config.get("hosts", {}).items()
        ]
        self.__short = short
        self.__error = error
        self.__short_error = short_error
        self.__hostname = hostname
        self.__custom_only = custom_only
        self.__no_custom = no_custom

    def __format_hostname(self, hostname, distro):
        color_codes = {
            "opensuse": "\033[1;92m",
            "opensuse-tumbleweed": "\033[1;92m",
            "opensuse-leap": "\033[1;92m",
            "redhat": "\033[1;91m",
            "rocky": "\033[1;91m",
            "almalinux": "\033[1;91m",
            "debian": "\033[1;95m",
        }
        color_code = color_codes.get(distro, "\033[1m")
        return f"{color_code}{hostname}\033[0m"

    def __format_service_status(self, status):
        status_colors = {
            "active": "\033[1;92m",
            "failed": "\033[1;91m",
            "inactive": "\033[1;94m",
            "activating": "\033[1;93m",
            "deactivating": "\033[1;93m",
        }
        color_code = status_colors.get(status, "\033[1m")
        return f"{color_code}{status}\033[0m"

    def __get_distro_id(self):
        try:
            stdin, stdout, stderr = self.__ssh_client.exec_command(
                "grep ^ID= /etc/os-release | awk -F= '{print $2}' | tr -d '\"'"
            )
            return stdout.read().decode("utf-8").strip()
        except Exception as e:
            print(f"Error fetching distro ID: {e}", file=sys.stderr)
            return "unknown"

    def __get_fqdn(self):
        try:
            stdin, stdout, stderr = self.__ssh_client.exec_command("hostname")
            return stdout.read().decode("utf-8").strip()
        except Exception as e:
            print(f"Error fetching FQDN: {e}", file=sys.stderr)
            return "unknown"

    def __check_default(self, services, custom_cmds=None):
        services_list = " ".join(services)
        if self.__sudo:
            services_cmd = "sudo /usr/bin/systemctl status " + services_list
        else:
            services_cmd = "/usr/bin/systemctl status " + services_list
        try:
            stdin, stdout, stderr = self.__ssh_client.exec_command(services_cmd)
            output_log = stdout.read().decode("utf-8")
            error_log = stderr.read().decode("utf-8")
            print(output_log)
            if error_log:
                print(error_log, file=sys.stderr)
        except Exception as e:
            print(f"Error checking default services: {e}", file=sys.stderr)

        if custom_cmds and not self.__no_custom:
            self.__check_custom(custom_cmds)

    def __check_custom(self, custom_cmds):
        for custom_cmd in custom_cmds:
            try:
                if self.__sudo:
                    custom_cmd = "sudo" + custom_cmd
                stdin, stdout, stderr = self.__ssh_client.exec_command(custom_cmd)
                output_log = stdout.read().decode("utf-8")
                error_log = stderr.read().decode("utf-8")
                print(output_log)
                if error_log:
                    print(error_log, file=sys.stderr)
            except Exception as e:
                print(
                    f"Error executing custom command '{custom_cmd}': {e}",
                    file=sys.stderr,
                )

    def __check_short(self, services):
        longest_service_name = max(len(service) for service in services)

        if self.__short or self.__short_error:
            print(
                f"\033[1m{'Service name'.ljust(longest_service_name)}\t:\tStatus\033[0m"
            )

        for service in services:
            status = self.__get_service_status(service)

            if self.__short or self.__short_error:
                print(
                    f"{service.ljust(longest_service_name)}\t:\t{self.__format_service_status(status)}"
                )

            if (self.__short_error or self.__error) and status == "failed":
                if self.__short_error and status == "failed":
                    print("")
                print(f"\t\033[1;91m{service}\033[0m")
                if self.__sudo:
                    short_error_cmd = (
                        f"sudo /usr/bin/journalctl -n{self.__lines} -u " + service
                    )
                else:
                    short_error_cmd = (
                        f"/usr/bin/journalctl -n{self.__lines} -u " + service
                    )
                stdin, stdout, stderr = self.__ssh_client.exec_command(short_error_cmd)

                print(stdout.read().decode("utf-8"))

    def __get_service_status(self, service_name):
        if self.__sudo:
            status_cmd = "sudo /usr/bin/systemctl is-active " + service_name
        else:
            status_cmd = "/usr/bin/systemctl is-active " + service_name
        try:
            # Check the active status
            stdin, stdout, stderr = self.__ssh_client.exec_command(status_cmd)
            return stdout.read().decode("utf-8").strip()
        except Exception as e:
            print(
                f"Error fetching service status for {service_name}: {e}",
                file=sys.stderr,
            )
            return "unknown"

    def check(self):
        hosts_to_check = self.__hosts_list

        if self.__hostname is not None:
            hosts_to_check = [
                host
                for host in self.__hosts_list
                if host.get_hostname() == self.__hostname
            ]

        for host in hosts_to_check:
            try:
                hostname = host.get_hostname()
                services = host.get_services()
                custom_cmds = host.get_custom()

                if not self.__dryrun:
                    self.__ssh_client.connect(
                        hostname=hostname,
                        username=self.__user,
                        password=self.__password,
                    )

                    print(
                        f"\n{self.__format_hostname(self.__get_fqdn().upper(), self.__get_distro_id())}\n"
                    )

                    if self.__short or self.__short_error or self.__error:
                        self.__check_short(services)
                    elif self.__custom_only:
                        self.__check_custom(custom_cmds)
                    else:
                        self.__check_default(services, custom_cmds)

                    self.__ssh_client.close()
            except Exception as e:
                print(f"Error connecting to {host.hostname}: {e}", file=sys.stderr)


class Hosts:
    def __init__(self, hostname, services, custom=None):
        self.__hostname = hostname
        self.__services = services
        self.__custom = custom

    def get_hostname(self):
        return self.__hostname

    def get_services(self):
        return self.__services

    def get_custom(self):
        return self.__custom


def main():
    parser = argparse.ArgumentParser(description="Check services script")
    parser.add_argument("config_file", help="Path to the configuration file")
    parser.add_argument("--user", help="User to use for ssh")
    parser.add_argument("--password", help="Password to use for ssh")
    parser.add_argument(
        "--dryrun", action="store_true", help="Simulate without making any changes"
    )
    parser.add_argument(
        "--short", action="store_true", help="Only return the status of the services"
    )
    parser.add_argument(
        "--error",
        action="store_true",
        help="Return the status of only the failed services",
    )
    parser.add_argument(
        "--short-error",
        action="store_true",
        help="Like --short but includes last 5 lines of journal log for failed services",
    )
    parser.add_argument(
        "--lines",
        help="The number of lines you want to see from the journal for a failed service",
    )
    parser.add_argument(
        "--hostname",
        help="The name of a specific host from the yaml file",
    )
    parser.add_argument(
        "--custom_only",
        action="store_true",
        help="Only the custom commands to check the services",
    )
    parser.add_argument(
        "--no_custom",
        action="store_true",
        help="No custom commands to check the services",
    )
    args = parser.parse_args()

    config_file = args.config_file
    if not os.path.isfile(config_file):
        print(f"Config file not found: {config_file}")
        sys.exit(1)

    check_services = CheckServices(
        config_file,
        user=args.user,
        password=args.password,
        dryrun=args.dryrun,
        short=args.short,
        error=args.error,
        short_error=args.short_error,
        lines=args.lines,
        hostname=args.hostname,
        custom_only=args.custom_only,
        no_custom=args.no_custom,
    )
    check_services.check()


if __name__ == "__main__":
    main()
