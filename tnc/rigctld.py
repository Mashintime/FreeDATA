#!/usr/bin/env python3
# class taken from darsidelemm
# rigctl - https://github.com/darksidelemm/rotctld-web-gui/blob/master/rotatorgui.py#L35
#
# modified and adjusted to FreeDATA needs by DJ2LS

import socket
import time
import static
import structlog

# set global hamlib version
hamlib_version = 0


class radio:
    """rigctld (hamlib) communication class"""

    # Note: This is a massive hack.

    log = structlog.get_logger("radio (rigctld)")

    def __init__(self, hostname="localhost", port=4532, poll_rate=5, timeout=5):
        """Open a connection to rigctld, and test it for validity"""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # self.sock.settimeout(timeout)

        self.connected = False
        self.hostname = hostname
        self.port = port
        self.connection_attempts = 5

    def open_rig(
        self,
        devicename,
        deviceport,
        hamlib_ptt_type,
        serialspeed,
        pttport,
        data_bits,
        stop_bits,
        handshake,
        rigctld_ip,
        rigctld_port,
    ):
        """

        Args:
          devicename:
          deviceport:
          hamlib_ptt_type:
          serialspeed:
          pttport:
          data_bits:
          stop_bits:
          handshake:
          rigctld_ip:
          rigctld_port:

        Returns:

        """
        self.hostname = rigctld_ip
        self.port = int(rigctld_port)

        if self.connect():
            self.log.debug("Rigctl initialized")
            return True

        self.log.error(
            "[RIGCTLD] Can't connect to rigctld!", ip=self.hostname, port=self.port
        )
        return False

    def connect(self):
        """Connect to rigctld instance"""
        if not self.connected:
            try:
                self.connection = socket.create_connection((self.hostname, self.port))
                self.connected = True
                self.log.info(
                    "[RIGCTLD] Connected to rigctld!", ip=self.hostname, port=self.port
                )
                return True
            except Exception as err:
                # ConnectionRefusedError: [Errno 111] Connection refused
                self.close_rig()
                self.log.warning(
                    "[RIGCTLD] Connection to rigctld refused! Reconnect...",
                    ip=self.hostname,
                    port=self.port,
                    e=err,
                )
                return False

    def close_rig(self):
        """ """
        self.sock.close()
        self.connected = False

    def send_command(self, command) -> bytes:
        """Send a command to the connected rotctld instance,
            and return the return value.

        Args:
          command:

        """
        if self.connected:
            try:
                self.connection.sendall(command + b"\n")
            except Exception:
                self.log.warning(
                    "[RIGCTLD] Command not executed!",
                    command=command,
                    ip=self.hostname,
                    port=self.port,
                )
                self.connected = False

            try:
                return self.connection.recv(1024)
            except Exception:
                self.log.warning(
                    "[RIGCTLD] No command response!",
                    command=command,
                    ip=self.hostname,
                    port=self.port,
                )
                self.connected = False
        else:

            # reconnecting....
            time.sleep(0.5)
            self.connect()

        return b""

    def get_status(self):
        """ """
        return "connected" if self.connected else "unknown/disconnected"
    def get_mode(self):
        """ """
        try:
            data = self.send_command(b"m")
            data = data.split(b"\n")
            mode = data[0]
            return mode.decode("utf-8")
        except Exception:
            return 0

    def get_bandwidth(self):
        """ """
        try:
            data = self.send_command(b"m")
            data = data.split(b"\n")
            bandwidth = data[1]
            return bandwidth.decode("utf-8")
        except Exception:
            return 0

    def get_frequency(self):
        """ """
        try:
            frequency = self.send_command(b"f")
            return frequency.decode("utf-8")
        except Exception:
            return 0

    def get_ptt(self):
        """ """
        try:
            return self.send_command(b"t")
        except Exception:
            return False

    def set_ptt(self, state):
        """

        Args:
          state:

        Returns:

        """
        try:
            if state:
                self.send_command(b"T 1")
            else:
                self.send_command(b"T 0")
            return state
        except Exception:
            return False
