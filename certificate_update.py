#!/usr/bin/env python3
import argparse
import os
import subprocess
import yaml
import sys
from logger import Logger

class CertUpdate:
    def __init__(self, config_file, dryrun=False):
        self.__interactive = sys.stdin.isatty()
        self.__dryrun = dryrun
        self.__logger = Logger()

        with open(config_file) as file:
            config = yaml.safe_load(file)
            source_location = config.get("source_location")
            certificate_name = config.get("name")
            servers = config.get("servers")

        #for server, directories, services in servers.items():
            
class Certificate:
    def __init__(self, servername, file=None, type=None):
        self.__file_list = []
        self.__servername = servername
        self.__logger = Logger()

        if file is not None and type is not None:
            self.__file_list.append(file, type)
        elif file is not None and type is None:
            self.__logger.log_error('Can not have a file without a type')
        elif file is None and type is not None:
            self.__logger.log_error('Can not have a type without a file')

    def add_file(self, file, type):
        self.__file_list.append(file,type)

    def set_servername(self, servername):
        self.__servername = servername

    def get_servername(self):
        return self.__servername

    def get_file_list(self):
        return self.__file_list
            
