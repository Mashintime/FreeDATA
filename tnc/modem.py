#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Dec 23 07:04:24 2020

@author: DJ2LS
"""

# pylint: disable=invalid-name, line-too-long, c-extension-no-member
# pylint: disable=import-outside-toplevel

import atexit
import ctypes
import os
import sys
import threading
import time
from collections import deque
import wave
import codec2
import itertools
import numpy as np
import sock
import sounddevice as sd
import static
from static import ARQ, AudioParam, Beacon, Channel, Daemon, HamlibParam, ModemParam, Station, Statistics, TCIParam, TNC
from static import FRAME_TYPE
import structlog
import ujson as json
import tci
# FIXME: used for def transmit_morse
# import cw
from queues import DATA_QUEUE_RECEIVED, MODEM_RECEIVED_QUEUE, MODEM_TRANSMIT_QUEUE, RIGCTLD_COMMAND_QUEUE, \
    AUDIO_RECEIVED_QUEUE, AUDIO_TRANSMIT_QUEUE, MESH_RECEIVED_QUEUE

TESTMODE = False
RXCHANNEL = ""
TXCHANNEL = ""

TNC.transmitting = False

# Receive only specific modes to reduce CPU load
RECEIVE_SIG0 = True
RECEIVE_SIG1 = False
RECEIVE_DATAC1 = False
RECEIVE_DATAC3 = False
RECEIVE_DATAC4 = False


# state buffer

SIG0_DATAC13_STATE = []
SIG1_DATAC13_STATE = []
DAT0_DATAC1_STATE = []
DAT0_DATAC3_STATE = []
DAT0_DATAC4_STATE = []

FSK_LDPC0_STATE = []
FSK_LDPC1_STATE = []


class RF:
    """Class to encapsulate interactions between the audio device and codec2"""

    log = structlog.get_logger("RF")

    def __init__(self) -> None:
        """ """
        self.sampler_avg = 0
        self.buffer_avg = 0

        self.AUDIO_SAMPLE_RATE_RX = 48000
        self.AUDIO_SAMPLE_RATE_TX = 48000
        self.MODEM_SAMPLE_RATE = codec2.api.FREEDV_FS_8000

        self.AUDIO_FRAMES_PER_BUFFER_RX = 2400 * 2  # 8192
        # 8192 Let's do some tests with very small chunks for TX
        self.AUDIO_FRAMES_PER_BUFFER_TX = 1200 if HamlibParam.hamlib_radiocontrol in ["tci"] else 2400 * 2
        # 8 * (self.AUDIO_SAMPLE_RATE_RX/self.MODEM_SAMPLE_RATE) == 48
        self.AUDIO_CHANNELS = 1
        self.MODE = 0

        # Locking state for mod out so buffer will be filled before we can use it
        # https://github.com/DJ2LS/FreeDATA/issues/127
        # https://github.com/DJ2LS/FreeDATA/issues/99
        self.mod_out_locked = True

        # Make sure our resampler will work
        assert (self.AUDIO_SAMPLE_RATE_RX / self.MODEM_SAMPLE_RATE) == codec2.api.FDMDV_OS_48  # type: ignore

        # init codec2 resampler
        self.resampler = codec2.resampler()

        self.modem_transmit_queue = MODEM_TRANSMIT_QUEUE
        self.modem_received_queue = MODEM_RECEIVED_QUEUE

        self.audio_received_queue = AUDIO_RECEIVED_QUEUE
        self.audio_transmit_queue = AUDIO_TRANSMIT_QUEUE

        # Init FIFO queue to store modulation out in
        self.modoutqueue = deque()

        # Define fft_data buffer
        self.fft_data = bytes()

        # Open codec2 instances

        # DATAC13
        # SIGNALLING MODE 0 - Used for Connecting - Payload 14 Bytes
        self.sig0_datac13_freedv, \
                self.sig0_datac13_bytes_per_frame, \
                self.sig0_datac13_bytes_out, \
                self.sig0_datac13_buffer, \
                self.sig0_datac13_nin = \
                self.init_codec2_mode(codec2.FREEDV_MODE.datac13.value, None)

        # DATAC13
        # SIGNALLING MODE 1 - Used for ACK/NACK - Payload 5 Bytes
        self.sig1_datac13_freedv, \
                self.sig1_datac13_bytes_per_frame, \
                self.sig1_datac13_bytes_out, \
                self.sig1_datac13_buffer, \
                self.sig1_datac13_nin = \
                self.init_codec2_mode(codec2.FREEDV_MODE.datac13.value, None)

        # DATAC1
        self.dat0_datac1_freedv, \
                self.dat0_datac1_bytes_per_frame, \
                self.dat0_datac1_bytes_out, \
                self.dat0_datac1_buffer, \
                self.dat0_datac1_nin = \
                self.init_codec2_mode(codec2.FREEDV_MODE.datac1.value, None)

        # DATAC3
        self.dat0_datac3_freedv, \
                self.dat0_datac3_bytes_per_frame, \
                self.dat0_datac3_bytes_out, \
                self.dat0_datac3_buffer, \
                self.dat0_datac3_nin = \
                self.init_codec2_mode(codec2.FREEDV_MODE.datac3.value, None)

        # DATAC4
        self.dat0_datac4_freedv, \
                self.dat0_datac4_bytes_per_frame, \
                self.dat0_datac4_bytes_out, \
                self.dat0_datac4_buffer, \
                self.dat0_datac4_nin = \
                self.init_codec2_mode(codec2.FREEDV_MODE.datac4.value, None)


        # FSK LDPC - 0
        self.fsk_ldpc_freedv_0, \
                self.fsk_ldpc_bytes_per_frame_0, \
                self.fsk_ldpc_bytes_out_0, \
                self.fsk_ldpc_buffer_0, \
                self.fsk_ldpc_nin_0 = \
                self.init_codec2_mode(
                codec2.FREEDV_MODE.fsk_ldpc.value,
                codec2.api.FREEDV_MODE_FSK_LDPC_0_ADV
            )

        # FSK LDPC - 1
        self.fsk_ldpc_freedv_1, \
                self.fsk_ldpc_bytes_per_frame_1, \
                self.fsk_ldpc_bytes_out_1, \
                self.fsk_ldpc_buffer_1, \
                self.fsk_ldpc_nin_1 = \
                self.init_codec2_mode(
                codec2.FREEDV_MODE.fsk_ldpc.value,
                codec2.api.FREEDV_MODE_FSK_LDPC_1_ADV
            )

        # INIT TX MODES - here we need all modes. 
        self.freedv_datac0_tx = open_codec2_instance(codec2.FREEDV_MODE.datac0.value)
        self.freedv_datac1_tx = open_codec2_instance(codec2.FREEDV_MODE.datac1.value)
        self.freedv_datac3_tx = open_codec2_instance(codec2.FREEDV_MODE.datac3.value)
        self.freedv_datac4_tx = open_codec2_instance(codec2.FREEDV_MODE.datac4.value)
        self.freedv_datac13_tx = open_codec2_instance(codec2.FREEDV_MODE.datac13.value)
        self.freedv_ldpc0_tx = open_codec2_instance(codec2.FREEDV_MODE.fsk_ldpc_0.value)
        self.freedv_ldpc1_tx = open_codec2_instance(codec2.FREEDV_MODE.fsk_ldpc_1.value)
        
        # --------------------------------------------CREATE PORTAUDIO INSTANCE
        if not TESTMODE and not HamlibParam.hamlib_radiocontrol in ["tci"]:
            try:
                self.stream = sd.RawStream(
                    channels=1,
                    dtype="int16",
                    callback=self.callback,
                    device=(AudioParam.audio_input_device, AudioParam.audio_output_device),
                    samplerate=self.AUDIO_SAMPLE_RATE_RX,
                    blocksize=4800,
                )
                atexit.register(self.stream.stop)
                self.log.info("[MDM] init: opened audio devices")
            except Exception as err:
                self.log.error("[MDM] init: can't open audio device. Exit", e=err)
                sys.exit(1)

            try:
                self.log.debug("[MDM] init: starting pyaudio callback")
                # self.audio_stream.start_stream(
                self.stream.start()
            except Exception as err:
                self.log.error("[MDM] init: starting pyaudio callback failed", e=err)

        elif not TESTMODE:
            # placeholder area for processing audio via TCI
            # https://github.com/maksimus1210/TCI
            self.log.warning("[MDM] [TCI] Not yet fully implemented", ip=TCIParam.ip, port=TCIParam.port)

            # we are trying this by simulating an audio stream Object like with mkfifo
            class Object:
                """An object for simulating audio stream"""
                active = True

            self.stream = Object()

            # lets init TCI module
            self.tci_module = tci.TCICtrl()

            tci_rx_callback_thread = threading.Thread(
                target=self.tci_rx_callback,
                name="TCI RX CALLBACK THREAD",
                daemon=True,
            )
            tci_rx_callback_thread.start()

            # let's start the audio tx callback
            self.log.debug("[MDM] Starting tci tx callback thread")
            tci_tx_callback_thread = threading.Thread(
                target=self.tci_tx_callback,
                name="TCI TX CALLBACK THREAD",
                daemon=True,
            )
            tci_tx_callback_thread.start()

        else:

            class Object:
                """An object for simulating audio stream"""
                active = True

            self.stream = Object()

            # Create mkfifo buffers
            try:
                os.mkfifo(RXCHANNEL)
                os.mkfifo(TXCHANNEL)
            except Exception as err:
                self.log.info(f"[MDM] init:mkfifo: Exception: {err}")

            mkfifo_write_callback_thread = threading.Thread(
                target=self.mkfifo_write_callback,
                name="MKFIFO WRITE CALLBACK THREAD",
                daemon=True,
            )
            mkfifo_write_callback_thread.start()

            self.log.debug("[MDM] Starting mkfifo_read_callback")
            mkfifo_read_callback_thread = threading.Thread(
                target=self.mkfifo_read_callback,
                name="MKFIFO READ CALLBACK THREAD",
                daemon=True,
            )
            mkfifo_read_callback_thread.start()

        # --------------------------------------------INIT AND OPEN HAMLIB
        # Check how we want to control the radio
        if HamlibParam.hamlib_radiocontrol == "rigctld":
            import rigctld as rig
        elif HamlibParam.hamlib_radiocontrol == "tci":
            self.radio = self.tci_module
        else:
            import rigdummy as rig

        if not HamlibParam.hamlib_radiocontrol in ["tci"]:
            self.radio = rig.radio()
            self.radio.open_rig(
                rigctld_ip=HamlibParam.hamlib_rigctld_ip,
                rigctld_port=HamlibParam.hamlib_rigctld_port,
            )

        # --------------------------------------------START DECODER THREAD
        if AudioParam.enable_fft:
            fft_thread = threading.Thread(
                target=self.calculate_fft, name="FFT_THREAD", daemon=True
            )
            fft_thread.start()

        if TNC.enable_fsk:
            audio_thread_fsk_ldpc0 = threading.Thread(
                target=self.audio_fsk_ldpc_0, name="AUDIO_THREAD FSK LDPC0", daemon=True
            )
            audio_thread_fsk_ldpc0.start()

            audio_thread_fsk_ldpc1 = threading.Thread(
                target=self.audio_fsk_ldpc_1, name="AUDIO_THREAD FSK LDPC1", daemon=True
            )
            audio_thread_fsk_ldpc1.start()

        else:
            audio_thread_sig0_datac13 = threading.Thread(
                target=self.audio_sig0_datac13, name="AUDIO_THREAD DATAC13 - 0", daemon=True
            )
            audio_thread_sig0_datac13.start()

            audio_thread_sig1_datac13 = threading.Thread(
                target=self.audio_sig1_datac13, name="AUDIO_THREAD DATAC13 - 1", daemon=True
            )
            audio_thread_sig1_datac13.start()

            audio_thread_dat0_datac1 = threading.Thread(
                target=self.audio_dat0_datac1, name="AUDIO_THREAD DATAC1", daemon=True
            )
            audio_thread_dat0_datac1.start()

            audio_thread_dat0_datac3 = threading.Thread(
                target=self.audio_dat0_datac3, name="AUDIO_THREAD DATAC3", daemon=True
            )
            audio_thread_dat0_datac3.start()

            audio_thread_dat0_datac4 = threading.Thread(
                target=self.audio_dat0_datac4, name="AUDIO_THREAD DATAC4", daemon=True
            )
            audio_thread_dat0_datac4.start()

        hamlib_thread = threading.Thread(
            target=self.update_rig_data, name="HAMLIB_THREAD", daemon=True
        )
        hamlib_thread.start()

        hamlib_set_thread = threading.Thread(
            target=self.set_rig_data, name="HAMLIB_SET_THREAD", daemon=True
        )
        hamlib_set_thread.start()

        # self.log.debug("[MDM] Starting worker_receive")
        worker_received = threading.Thread(
            target=self.worker_received, name="WORKER_THREAD", daemon=True
        )
        worker_received.start()

        worker_transmit = threading.Thread(
            target=self.worker_transmit, name="WORKER_THREAD", daemon=True
        )
        worker_transmit.start()

    # --------------------------------------------------------------------------------------------------------
    def tci_tx_callback(self) -> None:
        """
        Callback for TCI TX
        """
        while True:
            threading.Event().wait(0.01)

            if len(self.modoutqueue) > 0 and not self.mod_out_locked:
                HamlibParam.ptt_state = self.radio.set_ptt(True)
                jsondata = {"ptt": "True"}
                data_out = json.dumps(jsondata)
                sock.SOCKET_QUEUE.put(data_out)

                data_out = self.modoutqueue.popleft()
                self.tci_module.push_audio(data_out)

    def tci_rx_callback(self) -> None:
        """
        Callback for TCI RX

        data_in48k must be filled with 48000Hz audio raw data

        """

        while True:
            threading.Event().wait(0.01)

            x = self.audio_received_queue.get()
            x = np.frombuffer(x, dtype=np.int16)
            # x = self.resampler.resample48_to_8(x)

            self.fft_data = x

            length_x = len(x)
            for data_buffer, receive in [
                (self.sig0_datac13_buffer, RECEIVE_SIG0),
                (self.sig1_datac13_buffer, RECEIVE_SIG1),
                (self.dat0_datac1_buffer, RECEIVE_DATAC1),
                (self.dat0_datac3_buffer, RECEIVE_DATAC3),
                (self.dat0_datac4_buffer, RECEIVE_DATAC4),
                (self.fsk_ldpc_buffer_0, TNC.enable_fsk),
                (self.fsk_ldpc_buffer_1, TNC.enable_fsk),
            ]:
                if (
                        not (data_buffer.nbuffer + length_x) > data_buffer.size
                        and receive
                ):
                    data_buffer.push(x)

    def mkfifo_read_callback(self) -> None:
        """
        Support testing by reading the audio data from a pipe and
        depositing the data into the codec data buffers.
        """
        while True:
            threading.Event().wait(0.01)
            # -----read
            data_in48k = bytes()
            with open(RXCHANNEL, "rb") as fifo:
                for line in fifo:
                    data_in48k += line

                    while len(data_in48k) >= 48:
                        x = np.frombuffer(data_in48k[:48], dtype=np.int16)
                        x = self.resampler.resample48_to_8(x)
                        data_in48k = data_in48k[48:]

                        length_x = len(x)
                        for data_buffer, receive in [
                            (self.sig0_datac13_buffer, RECEIVE_SIG0),
                            (self.sig1_datac13_buffer, RECEIVE_SIG1),
                            (self.dat0_datac1_buffer, RECEIVE_DATAC1),
                            (self.dat0_datac3_buffer, RECEIVE_DATAC3),
                            (self.dat0_datac4_buffer, RECEIVE_DATAC4),
                            (self.fsk_ldpc_buffer_0, TNC.enable_fsk),
                            (self.fsk_ldpc_buffer_1, TNC.enable_fsk),
                        ]:
                            if (
                                    not (data_buffer.nbuffer + length_x) > data_buffer.size
                                    and receive
                            ):
                                data_buffer.push(x)

    def mkfifo_write_callback(self) -> None:
        """Support testing by writing the audio data to a pipe."""
        while True:
            threading.Event().wait(0.01)

            # -----write
            if len(self.modoutqueue) > 0 and not self.mod_out_locked:
                data_out48k = self.modoutqueue.popleft()
                # print(len(data_out48k))

                with open(TXCHANNEL, "wb") as fifo_write:
                    fifo_write.write(data_out48k)
                    fifo_write.flush()
                    fifo_write.flush()

    # --------------------------------------------------------------------
    def callback(self, data_in48k, outdata, frames, time, status) -> None:
        """
        Receive data into appropriate queue.

        Args:
            data_in48k: Incoming data received
            outdata: Container for the data returned
            frames: Number of frames
            time:
          status:

        """
        # self.log.debug("[MDM] callback")
        x = np.frombuffer(data_in48k, dtype=np.int16)
        x = self.resampler.resample48_to_8(x)

        # audio recording for debugging purposes
        if AudioParam.audio_record:
            # AudioParam.audio_record_file.write(x)
            AudioParam.audio_record_file.writeframes(x)

        # Avoid decoding when transmitting to reduce CPU
        # TODO: Overriding this for testing purposes
        # if not TNC.transmitting:
        length_x = len(x)
        # Avoid buffer overflow by filling only if buffer for
        # selected datachannel mode is not full
        for audiobuffer, receive, index in [
            (self.sig0_datac13_buffer, RECEIVE_SIG0, 0),
            (self.sig1_datac13_buffer, RECEIVE_SIG1, 1),
            (self.dat0_datac1_buffer, RECEIVE_DATAC1, 2),
            (self.dat0_datac3_buffer, RECEIVE_DATAC3, 3),
            (self.dat0_datac4_buffer, RECEIVE_DATAC4, 4),
            (self.fsk_ldpc_buffer_0, TNC.enable_fsk, 5),
            (self.fsk_ldpc_buffer_1, TNC.enable_fsk, 6),
        ]:
            if (audiobuffer.nbuffer + length_x) > audiobuffer.size:
                AudioParam.buffer_overflow_counter[index] += 1
            elif receive:
                audiobuffer.push(x)
        # end of "not TNC.transmitting" if block

        if not self.modoutqueue or self.mod_out_locked:
            data_out48k = np.zeros(frames, dtype=np.int16)
            self.fft_data = x
        else:
            if not HamlibParam.ptt_state:
                # TODO: Moved to this place for testing
                # Maybe we can avoid moments of silence before transmitting
                HamlibParam.ptt_state = self.radio.set_ptt(True)
                jsondata = {"ptt": "True"}
                data_out = json.dumps(jsondata)
                sock.SOCKET_QUEUE.put(data_out)

            data_out48k = self.modoutqueue.popleft()
            self.fft_data = data_out48k

        try:
            outdata[:] = data_out48k[:frames]
        except IndexError as err:
            self.log.debug(f"[MDM] callback: IndexError: {err}")

        # return (data_out48k, audio.pyaudio.paContinue)

    # --------------------------------------------------------------------
    def transmit(
            self, mode, repeats: int, repeat_delay: int, frames: bytearray
    ) -> None:
        """

        Args:
          mode:
          repeats:
          repeat_delay:
          frames:

        """
        self.reset_data_sync()

        if mode == codec2.FREEDV_MODE.datac0.value:
            freedv = self.freedv_datac0_tx
        elif mode == codec2.FREEDV_MODE.datac1.value:
            freedv = self.freedv_datac1_tx
        elif mode == codec2.FREEDV_MODE.datac3.value:
            freedv = self.freedv_datac3_tx
        elif mode == codec2.FREEDV_MODE.datac4.value:
            freedv = self.freedv_datac4_tx
        elif mode == codec2.FREEDV_MODE.datac13.value:
            freedv = self.freedv_datac13_tx
        elif mode == codec2.FREEDV_MODE.fsk_ldpc_0.value:
            freedv = self.freedv_ldpc0_tx
        elif mode == codec2.FREEDV_MODE.fsk_ldpc_1.value:
            freedv = self.freedv_ldpc1_tx
        else:
            return False

        TNC.transmitting = True
        # if we're transmitting FreeDATA signals, reset channel busy state
        ModemParam.channel_busy = False

        start_of_transmission = time.time()
        # TODO: Moved ptt toggle some steps before audio is ready for testing
        # Toggle ptt early to save some time and send ptt state via socket
        # HamlibParam.ptt_state = self.radio.set_ptt(True)
        # jsondata = {"ptt": "True"}
        # data_out = json.dumps(jsondata)
        # sock.SOCKET_QUEUE.put(data_out)

        # Open codec2 instance
        self.MODE = mode

        # Get number of bytes per frame for mode
        bytes_per_frame = int(codec2.api.freedv_get_bits_per_modem_frame(freedv) / 8)
        payload_bytes_per_frame = bytes_per_frame - 2

        # Init buffer for data
        n_tx_modem_samples = codec2.api.freedv_get_n_tx_modem_samples(freedv)
        mod_out = ctypes.create_string_buffer(n_tx_modem_samples * 2)

        # Init buffer for preample
        n_tx_preamble_modem_samples = codec2.api.freedv_get_n_tx_preamble_modem_samples(
            freedv
        )
        mod_out_preamble = ctypes.create_string_buffer(n_tx_preamble_modem_samples * 2)

        # Init buffer for postamble
        n_tx_postamble_modem_samples = (
            codec2.api.freedv_get_n_tx_postamble_modem_samples(freedv)
        )
        mod_out_postamble = ctypes.create_string_buffer(
            n_tx_postamble_modem_samples * 2
        )

        # Add empty data to handle ptt toggle time
        if ModemParam.tx_delay > 0:
            data_delay = int(self.MODEM_SAMPLE_RATE * (ModemParam.tx_delay / 1000))  # type: ignore
            mod_out_silence = ctypes.create_string_buffer(data_delay * 2)
            txbuffer = bytes(mod_out_silence)
        else:
            txbuffer = bytes()

        self.log.debug(
            "[MDM] TRANSMIT", mode=self.MODE, payload=payload_bytes_per_frame, delay=ModemParam.tx_delay
        )

        for _ in range(repeats):

            # Create modulation for all frames in the list
            for frame in frames:
                # Write preamble to txbuffer
                # codec2 fsk preamble may be broken -
                # at least it sounds like that, so we are disabling it for testing
                if self.MODE not in [
                    codec2.FREEDV_MODE.fsk_ldpc_0.value,
                    codec2.FREEDV_MODE.fsk_ldpc_1.value,
                ]:
                    # Write preamble to txbuffer
                    codec2.api.freedv_rawdatapreambletx(freedv, mod_out_preamble)
                    txbuffer += bytes(mod_out_preamble)

                # Create buffer for data
                # Use this if CRC16 checksum is required (DATAc1-3)
                buffer = bytearray(payload_bytes_per_frame)
                # Set buffersize to length of data which will be send
                buffer[: len(frame)] = frame  # type: ignore

                # Create crc for data frame -
                #   Use the crc function shipped with codec2
                #   to avoid CRC algorithm incompatibilities
                # Generate CRC16
                crc = ctypes.c_ushort(
                    codec2.api.freedv_gen_crc16(bytes(buffer), payload_bytes_per_frame)
                )
                # Convert crc to 2-byte (16-bit) hex string
                crc = crc.value.to_bytes(2, byteorder="big")
                # Append CRC to data buffer
                buffer += crc

                data = (ctypes.c_ubyte * bytes_per_frame).from_buffer_copy(buffer)
                # modulate DATA and save it into mod_out pointer
                codec2.api.freedv_rawdatatx(freedv, mod_out, data)
                txbuffer += bytes(mod_out)

                # codec2 fsk postamble may be broken -
                # at least it sounds like that, so we are disabling it for testing
                if self.MODE not in [
                    codec2.FREEDV_MODE.fsk_ldpc_0.value,
                    codec2.FREEDV_MODE.fsk_ldpc_1.value,
                ]:
                    # Write postamble to txbuffer
                    codec2.api.freedv_rawdatapostambletx(freedv, mod_out_postamble)
                    # Append postamble to txbuffer
                    txbuffer += bytes(mod_out_postamble)

            # Add delay to end of frames
            samples_delay = int(self.MODEM_SAMPLE_RATE * (repeat_delay / 1000))  # type: ignore
            mod_out_silence = ctypes.create_string_buffer(samples_delay * 2)
            txbuffer += bytes(mod_out_silence)

        # Re-sample back up to 48k (resampler works on np.int16)
        x = np.frombuffer(txbuffer, dtype=np.int16)

        # enable / disable AUDIO TUNE Feature / ALC correction
        if AudioParam.audio_auto_tune:
            if HamlibParam.alc == 0.0:
                AudioParam.tx_audio_level = AudioParam.tx_audio_level + 20
            elif 0.0 < HamlibParam.alc <= 0.1:
                print("0.0 < HamlibParam.alc <= 0.1")
                AudioParam.tx_audio_level = AudioParam.tx_audio_level + 2
                self.log.debug("[MDM] AUDIO TUNE", audio_level=str(AudioParam.tx_audio_level),
                               alc_level=str(HamlibParam.alc))
            elif 0.1 < HamlibParam.alc < 0.2:
                print("0.1 < HamlibParam.alc < 0.2")
                AudioParam.tx_audio_level = AudioParam.tx_audio_level
                self.log.debug("[MDM] AUDIO TUNE", audio_level=str(AudioParam.tx_audio_level),
                               alc_level=str(HamlibParam.alc))
            elif 0.2 < HamlibParam.alc < 0.99:
                print("0.2 < HamlibParam.alc < 0.99")
                AudioParam.tx_audio_level = AudioParam.tx_audio_level - 20
                self.log.debug("[MDM] AUDIO TUNE", audio_level=str(AudioParam.tx_audio_level),
                               alc_level=str(HamlibParam.alc))
            elif 1.0 >= HamlibParam.alc:
                print("1.0 >= HamlibParam.alc")
                AudioParam.tx_audio_level = AudioParam.tx_audio_level - 40
                self.log.debug("[MDM] AUDIO TUNE", audio_level=str(AudioParam.tx_audio_level),
                               alc_level=str(HamlibParam.alc))
            else:
                self.log.debug("[MDM] AUDIO TUNE", audio_level=str(AudioParam.tx_audio_level),
                               alc_level=str(HamlibParam.alc))
        x = set_audio_volume(x, AudioParam.tx_audio_level)

        if not HamlibParam.hamlib_radiocontrol in ["tci"]:
            txbuffer_out = self.resampler.resample8_to_48(x)
        else:
            txbuffer_out = x

        # Explicitly lock our usage of mod_out_queue if needed
        # This could avoid audio problems on slower CPU
        # we will fill our modout list with all data, then start
        # processing it in audio callback
        self.mod_out_locked = True

        # -------------------------------
        # add modulation to modout_queue
        self.enqueue_modulation(txbuffer_out)

        # Release our mod_out_lock, so we can use the queue
        self.mod_out_locked = False

        # we need to wait manually for tci processing
        if HamlibParam.hamlib_radiocontrol in ["tci"]:
            duration = len(txbuffer_out) / 8000
            timestamp_to_sleep = time.time() + duration
            self.log.debug("[MDM] TCI calculated duration", duration=duration)
            tci_timeout_reached = False
            #while time.time() < timestamp_to_sleep:
            #    threading.Event().wait(0.01)
        else:
            timestamp_to_sleep = time.time()
            # set tci timeout reached to True for overriding if not used
            tci_timeout_reached = True

        while self.modoutqueue or not tci_timeout_reached:
            if HamlibParam.hamlib_radiocontrol in ["tci"]:
                if time.time() < timestamp_to_sleep:
                    tci_timeout_reached = False
                else:
                    tci_timeout_reached = True

            threading.Event().wait(0.01)
            # if we're transmitting FreeDATA signals, reset channel busy state
            ModemParam.channel_busy = False

        HamlibParam.ptt_state = self.radio.set_ptt(False)

        # Push ptt state to socket stream
        jsondata = {"ptt": "False"}
        data_out = json.dumps(jsondata)
        sock.SOCKET_QUEUE.put(data_out)

        # After processing, set the locking state back to true to be prepared for next transmission
        self.mod_out_locked = True

        self.modem_transmit_queue.task_done()
        TNC.transmitting = False
        threading.Event().set()

        end_of_transmission = time.time()
        transmission_time = end_of_transmission - start_of_transmission
        self.log.debug("[MDM] ON AIR TIME", time=transmission_time)

    def transmit_morse(self, repeats, repeat_delay, frames):
        TNC.transmitting = True
        # if we're transmitting FreeDATA signals, reset channel busy state
        ModemParam.channel_busy = False
        self.log.debug(
            "[MDM] TRANSMIT", mode="MORSE"
        )
        start_of_transmission = time.time()

        txbuffer = cw.MorseCodePlayer().text_to_signal("DJ2LS-1")
        print(txbuffer)
        print(type(txbuffer))
        x = np.frombuffer(txbuffer, dtype=np.int16)
        print(type(x))
        txbuffer_out = x
        print(txbuffer_out)

        #if not HamlibParam.hamlib_radiocontrol in ["tci"]:
        #    txbuffer_out = self.resampler.resample8_to_48(x)
        #else:
        #    txbuffer_out = x

        self.mod_out_locked = True
        self.enqueue_modulation(txbuffer_out)
        self.mod_out_locked = False

        # we need to wait manually for tci processing
        if HamlibParam.hamlib_radiocontrol in ["tci"]:
            duration = len(txbuffer_out) / 8000
            timestamp_to_sleep = time.time() + duration
            self.log.debug("[MDM] TCI calculated duration", duration=duration)
            tci_timeout_reached = False
            #while time.time() < timestamp_to_sleep:
            #    threading.Event().wait(0.01)
        else:
            timestamp_to_sleep = time.time()
            # set tci timeout reached to True for overriding if not used
            tci_timeout_reached = True

        while self.modoutqueue or not tci_timeout_reached:
            if HamlibParam.hamlib_radiocontrol in ["tci"]:
                if time.time() < timestamp_to_sleep:
                    tci_timeout_reached = False
                else:
                    tci_timeout_reached = True

            threading.Event().wait(0.01)
            # if we're transmitting FreeDATA signals, reset channel busy state
            ModemParam.channel_busy = False




        HamlibParam.ptt_state = self.radio.set_ptt(False)

        # Push ptt state to socket stream
        jsondata = {"ptt": "False"}
        data_out = json.dumps(jsondata)
        sock.SOCKET_QUEUE.put(data_out)

        # After processing, set the locking state back to true to be prepared for next transmission
        self.mod_out_locked = True

        self.modem_transmit_queue.task_done()
        TNC.transmitting = False
        threading.Event().set()

        end_of_transmission = time.time()
        transmission_time = end_of_transmission - start_of_transmission
        self.log.debug("[MDM] ON AIR TIME", time=transmission_time)

    def enqueue_modulation(self, txbuffer_out):
        chunk_length = self.AUDIO_FRAMES_PER_BUFFER_TX  # 4800
        chunk = [
            txbuffer_out[i: i + chunk_length]
            for i in range(0, len(txbuffer_out), chunk_length)
        ]
        for c in chunk:
            # Pad the chunk, if needed
            if len(c) < chunk_length:
                delta = chunk_length - len(c)
                delta_zeros = np.zeros(delta, dtype=np.int16)
                c = np.append(c, delta_zeros)
                # self.log.debug("[MDM] mod out shorter than audio buffer", delta=delta)
            self.modoutqueue.append(c)


    def demodulate_audio(
            self,
            audiobuffer: codec2.audio_buffer,
            nin: int,
            freedv: ctypes.c_void_p,
            bytes_out,
            bytes_per_frame,
            state_buffer,
            mode_name,
    ) -> int:
        """
        De-modulate supplied audio stream with supplied codec2 instance.
        Decoded audio is placed into `bytes_out`.

        :param audiobuffer: Incoming audio
        :type audiobuffer: codec2.audio_buffer
        :param nin: Number of frames codec2 is expecting
        :type nin: int
        :param freedv: codec2 instance
        :type freedv: ctypes.c_void_p
        :param bytes_out: Demodulated audio
        :type bytes_out: _type_
        :param bytes_per_frame: Number of bytes per frame
        :type bytes_per_frame: int
        :param state_buffer: modem states
        :type state_buffer: int
        :param mode_name: mode name
        :type mode_name: str
        :return: NIN from freedv instance
        :rtype: int
        """
        nbytes = 0
        try:
            while self.stream.active:
                threading.Event().wait(0.01)
                while audiobuffer.nbuffer >= nin:
                    # demodulate audio
                    nbytes = codec2.api.freedv_rawdatarx(
                        freedv, bytes_out, audiobuffer.buffer.ctypes
                    )
                    # get current modem states and write to list
                    # 1 trial
                    # 2 sync
                    # 3 trial sync
                    # 6 decoded
                    # 10 error decoding == NACK
                    rx_status = codec2.api.freedv_get_rx_status(freedv)

                    if rx_status != 0:
                        # we need to disable this if in testmode as its causing problems with FIFO it seems
                        if not TESTMODE:
                            ModemParam.is_codec2_traffic = True

                        self.log.debug(
                            "[MDM] [demod_audio] modem state", mode=mode_name, rx_status=rx_status,
                            sync_flag=codec2.api.rx_sync_flags_to_text[rx_status]
                        )
                    else:
                        ModemParam.is_codec2_traffic = False

                    if rx_status == 10:
                        state_buffer.append(rx_status)

                    audiobuffer.pop(nin)
                    nin = codec2.api.freedv_nin(freedv)
                    if nbytes == bytes_per_frame:
                        print(bytes(bytes_out))

                        # process commands only if TNC.listen = True
                        if TNC.listen:


                            # ignore data channel opener frames for avoiding toggle states
                            # use case: opener already received, but ack got lost and we are receiving
                            # an opener again
                            if mode_name in ["sig1-datac13"] and int.from_bytes(bytes(bytes_out[:1]), "big") in [
                                FRAME_TYPE.ARQ_SESSION_OPEN.value,
                                FRAME_TYPE.ARQ_DC_OPEN_W.value,
                                FRAME_TYPE.ARQ_DC_OPEN_ACK_W.value,
                                FRAME_TYPE.ARQ_DC_OPEN_N.value,
                                FRAME_TYPE.ARQ_DC_OPEN_ACK_N.value
                            ]:
                                print("dropp")
                            elif int.from_bytes(bytes(bytes_out[:1]), "big") in [
                                FRAME_TYPE.MESH_BROADCAST.value,
                                FRAME_TYPE.MESH_SIGNALLING_PING.value,
                                FRAME_TYPE.MESH_SIGNALLING_PING_ACK.value,
                            ]:
                                self.log.debug(
                                    "[MDM] [demod_audio] moving data to mesh dispatcher", nbytes=nbytes
                                )
                                MESH_RECEIVED_QUEUE.put(bytes(bytes_out))

                            else:
                                self.log.debug(
                                    "[MDM] [demod_audio] Pushing received data to received_queue", nbytes=nbytes
                                )

                                self.modem_received_queue.put([bytes_out, freedv, bytes_per_frame])
                                self.get_scatter(freedv)
                                self.calculate_snr(freedv)
                                state_buffer = []
                        else:
                            self.log.warning(
                                "[MDM] [demod_audio] received frame but ignored processing",
                                listen=TNC.listen
                            )
        except Exception as e:
            self.log.warning("[MDM] [demod_audio] Stream not active anymore", e=e)
        return nin

    def init_codec2_mode(self, mode, adv):
        """
        Init codec2 and return some important parameters

        Args:
          self:
          mode:
          adv:

        Returns:
            c2instance, bytes_per_frame, bytes_out, audio_buffer, nin
        """
        if adv:
            # FSK Long-distance Parity Code 1 - data frames
            c2instance = ctypes.cast(
                codec2.api.freedv_open_advanced(
                    codec2.FREEDV_MODE.fsk_ldpc.value,
                    ctypes.byref(adv),
                ),
                ctypes.c_void_p,
            )
        else:

            # create codec2 instance
            c2instance = ctypes.cast(
                codec2.api.freedv_open(mode), ctypes.c_void_p
            )

        # set tuning range
        codec2.api.freedv_set_tuning_range(
            c2instance,
            ctypes.c_float(ModemParam.tuning_range_fmin),
            ctypes.c_float(ModemParam.tuning_range_fmax),
        )

        # get bytes per frame
        bytes_per_frame = int(
            codec2.api.freedv_get_bits_per_modem_frame(c2instance) / 8
        )

        # create byte out buffer
        bytes_out = ctypes.create_string_buffer(bytes_per_frame)

        # set initial frames per burst
        codec2.api.freedv_set_frames_per_burst(c2instance, 1)

        # init audio buffer
        audio_buffer = codec2.audio_buffer(2 * self.AUDIO_FRAMES_PER_BUFFER_RX)

        # get initial nin
        nin = codec2.api.freedv_nin(c2instance)

        # Additional Datac0-specific information - these are not referenced anywhere else.
        # self.sig0_datac0_payload_per_frame = self.sig0_datac0_bytes_per_frame - 2
        # self.sig0_datac0_n_nom_modem_samples = codec2.api.freedv_get_n_nom_modem_samples(
        #     self.sig0_datac0_freedv
        # )
        # self.sig0_datac0_n_tx_modem_samples = codec2.api.freedv_get_n_tx_modem_samples(
        #     self.sig0_datac0_freedv
        # )
        # self.sig0_datac0_n_tx_preamble_modem_samples = (
        #     codec2.api.freedv_get_n_tx_preamble_modem_samples(self.sig0_datac0_freedv)
        # )
        # self.sig0_datac0_n_tx_postamble_modem_samples = (
        #     codec2.api.freedv_get_n_tx_postamble_modem_samples(self.sig0_datac0_freedv)
        # )

        # return values
        return c2instance, bytes_per_frame, bytes_out, audio_buffer, nin

    def audio_sig0_datac13(self) -> None:
        """Receive data encoded with datac13 - 0"""
        self.sig0_datac13_nin = self.demodulate_audio(
            self.sig0_datac13_buffer,
            self.sig0_datac13_nin,
            self.sig0_datac13_freedv,
            self.sig0_datac13_bytes_out,
            self.sig0_datac13_bytes_per_frame,
            SIG0_DATAC13_STATE,
            "sig0-datac13"
        )

    def audio_sig1_datac13(self) -> None:
        """Receive data encoded with datac13 - 1"""
        self.sig1_datac13_nin = self.demodulate_audio(
            self.sig1_datac13_buffer,
            self.sig1_datac13_nin,
            self.sig1_datac13_freedv,
            self.sig1_datac13_bytes_out,
            self.sig1_datac13_bytes_per_frame,
            SIG1_DATAC13_STATE,
            "sig1-datac13"
        )

    def audio_dat0_datac4(self) -> None:
        """Receive data encoded with datac4"""
        self.dat0_datac4_nin = self.demodulate_audio(
            self.dat0_datac4_buffer,
            self.dat0_datac4_nin,
            self.dat0_datac4_freedv,
            self.dat0_datac4_bytes_out,
            self.dat0_datac4_bytes_per_frame,
            DAT0_DATAC4_STATE,
            "dat0-datac4"
        )

    def audio_dat0_datac1(self) -> None:
        """Receive data encoded with datac1"""
        self.dat0_datac1_nin = self.demodulate_audio(
            self.dat0_datac1_buffer,
            self.dat0_datac1_nin,
            self.dat0_datac1_freedv,
            self.dat0_datac1_bytes_out,
            self.dat0_datac1_bytes_per_frame,
            DAT0_DATAC1_STATE,
            "dat0-datac1"
        )

    def audio_dat0_datac3(self) -> None:
        """Receive data encoded with datac3"""
        self.dat0_datac3_nin = self.demodulate_audio(
            self.dat0_datac3_buffer,
            self.dat0_datac3_nin,
            self.dat0_datac3_freedv,
            self.dat0_datac3_bytes_out,
            self.dat0_datac3_bytes_per_frame,
            DAT0_DATAC3_STATE,
            "dat0-datac3"
        )

    def audio_fsk_ldpc_0(self) -> None:
        """Receive data encoded with FSK + LDPC0"""
        self.fsk_ldpc_nin_0 = self.demodulate_audio(
            self.fsk_ldpc_buffer_0,
            self.fsk_ldpc_nin_0,
            self.fsk_ldpc_freedv_0,
            self.fsk_ldpc_bytes_out_0,
            self.fsk_ldpc_bytes_per_frame_0,
            FSK_LDPC0_STATE,
            "fsk_ldpc0",
        )

    def audio_fsk_ldpc_1(self) -> None:
        """Receive data encoded with FSK + LDPC1"""
        self.fsk_ldpc_nin_1 = self.demodulate_audio(
            self.fsk_ldpc_buffer_1,
            self.fsk_ldpc_nin_1,
            self.fsk_ldpc_freedv_1,
            self.fsk_ldpc_bytes_out_1,
            self.fsk_ldpc_bytes_per_frame_1,
            FSK_LDPC1_STATE,
            "fsk_ldpc1",
        )

    def worker_transmit(self) -> None:
        """Worker for FIFO queue for processing frames to be transmitted"""
        while True:
            # print queue size for debugging purposes
            # TODO: Lets check why we have several frames in our transmit queue which causes sometimes a double transmission
            # we could do a cleanup after a transmission so theres no reason sending twice
            queuesize = self.modem_transmit_queue.qsize()
            self.log.debug("[MDM] self.modem_transmit_queue", qsize=queuesize)
            data = self.modem_transmit_queue.get()

            if data[0] in ["morse"]:
                self.transmit_morse(repeats=data[1], repeat_delay=data[2], frames=data[3])
            else:
                self.transmit(
                    mode=data[0], repeats=data[1], repeat_delay=data[2], frames=data[3]
                )
            # self.modem_transmit_queue.task_done()

    def worker_received(self) -> None:
        """Worker for FIFO queue for processing received frames"""
        while True:
            data = self.modem_received_queue.get()
            self.log.debug("[MDM] worker_received: received data!")
            # data[0] = bytes_out
            # data[1] = freedv session
            # data[2] = bytes_per_frame
            DATA_QUEUE_RECEIVED.put([data[0], data[1], data[2]])
            self.modem_received_queue.task_done()

    def get_frequency_offset(self, freedv: ctypes.c_void_p) -> float:
        """
        Ask codec2 for the calculated (audio) frequency offset of the received signal.
        Side-effect: sets ModemParam.frequency_offset

        :param freedv: codec2 instance to query
        :type freedv: ctypes.c_void_p
        :return: Offset of audio frequency in Hz
        :rtype: float
        """
        modemStats = codec2.MODEMSTATS()
        codec2.api.freedv_get_modem_extended_stats(freedv, ctypes.byref(modemStats))
        offset = round(modemStats.foff) * (-1)
        ModemParam.frequency_offset = offset
        return offset

    def get_scatter(self, freedv: ctypes.c_void_p) -> None:
        """
        Ask codec2 for data about the received signal and calculate the scatter plot.
        Side-effect: sets ModemParam.scatter

        :param freedv: codec2 instance to query
        :type freedv: ctypes.c_void_p
        """
        if not ModemParam.enable_scatter:
            return

        modemStats = codec2.MODEMSTATS()
        ctypes.cast(
            codec2.api.freedv_get_modem_extended_stats(freedv, ctypes.byref(modemStats)),
            ctypes.c_void_p,
        )

        scatterdata = []
        # original function before itertool
        # for i in range(codec2.MODEM_STATS_NC_MAX):
        #    for j in range(1, codec2.MODEM_STATS_NR_MAX, 2):
        #        # print(f"{modemStats.rx_symbols[i][j]} - {modemStats.rx_symbols[i][j]}")
        #        xsymbols = round(modemStats.rx_symbols[i][j - 1] // 1000)
        #        ysymbols = round(modemStats.rx_symbols[i][j] // 1000)
        #        if xsymbols != 0.0 and ysymbols != 0.0:
        #            scatterdata.append({"x": str(xsymbols), "y": str(ysymbols)})

        for i, j in itertools.product(range(codec2.MODEM_STATS_NC_MAX), range(1, codec2.MODEM_STATS_NR_MAX, 2)):
            # print(f"{modemStats.rx_symbols[i][j]} - {modemStats.rx_symbols[i][j]}")
            xsymbols = round(modemStats.rx_symbols[i][j - 1] // 1000)
            ysymbols = round(modemStats.rx_symbols[i][j] // 1000)
            if xsymbols != 0.0 and ysymbols != 0.0:
                scatterdata.append({"x": str(xsymbols), "y": str(ysymbols)})

        # Send all the data if we have too-few samples, otherwise send a sampling
        if 150 > len(scatterdata) > 0:
            ModemParam.scatter = scatterdata
        else:
            # only take every tenth data point
            ModemParam.scatter = scatterdata[::10]

    def calculate_snr(self, freedv: ctypes.c_void_p) -> float:
        """
        Ask codec2 for data about the received signal and calculate
        the signal-to-noise ratio.
        Side-effect: sets ModemParam.snr

        :param freedv: codec2 instance to query
        :type freedv: ctypes.c_void_p
        :return: Signal-to-noise ratio of the decoded data
        :rtype: float
        """
        try:
            modem_stats_snr = ctypes.c_float()
            modem_stats_sync = ctypes.c_int()

            codec2.api.freedv_get_modem_stats(
                freedv, ctypes.byref(modem_stats_sync), ctypes.byref(modem_stats_snr)
            )
            modem_stats_snr = modem_stats_snr.value
            modem_stats_sync = modem_stats_sync.value

            snr = round(modem_stats_snr, 1)
            self.log.info("[MDM] calculate_snr: ", snr=snr)
            ModemParam.snr = snr
            # ModemParam.snr = np.clip(
            #    snr, -127, 127
            # )  # limit to max value of -128/128 as a possible fix of #188
            return ModemParam.snr
        except Exception as err:
            self.log.error(f"[MDM] calculate_snr: Exception: {err}")
            ModemParam.snr = 0
            return ModemParam.snr

    def set_rig_data(self) -> None:
        """
            Set rigctld parameters like frequency, mode
            THis needs to be processed in a queue
        """
        while True:
            cmd = RIGCTLD_COMMAND_QUEUE.get()
            if cmd[0] == "set_frequency":
                # [1] = Frequency
                self.radio.set_frequency(cmd[1])
            if cmd[0] == "set_mode":
                # [1] = Mode
                self.radio.set_mode(cmd[1])

    def update_rig_data(self) -> None:
        """
        Request information about the current state of the radio via hamlib
        Side-effect: sets
          - HamlibParam.hamlib_frequency
          - HamlibParam.hamlib_mode
          - HamlibParam.hamlib_bandwidth
        """
        while True:
            try:
                # this looks weird, but is necessary for avoiding rigctld packet colission sock
                threading.Event().wait(0.25)
                HamlibParam.hamlib_frequency = self.radio.get_frequency()
                threading.Event().wait(0.1)
                HamlibParam.hamlib_mode = self.radio.get_mode()
                threading.Event().wait(0.1)
                HamlibParam.hamlib_bandwidth = self.radio.get_bandwidth()
                threading.Event().wait(0.1)
                HamlibParam.hamlib_status = self.radio.get_status()
                threading.Event().wait(0.1)
                if TNC.transmitting:
                    HamlibParam.alc = self.radio.get_alc()
                    threading.Event().wait(0.1)
                # HamlibParam.hamlib_rf = self.radio.get_level()
                # threading.Event().wait(0.1)
                HamlibParam.hamlib_strength = self.radio.get_strength()

                # print(f"ALC: {HamlibParam.alc}, RF: {HamlibParam.hamlib_rf}, STRENGTH: {HamlibParam.hamlib_strength}")
            except Exception as e:
                self.log.warning(
                    "[MDM] error getting radio data",
                    e=e,
                )
                threading.Event().wait(1)
    def calculate_fft(self) -> None:
        """
        Calculate an average signal strength of the channel to assess
        whether the channel is "busy."
        """
        # Initialize channel_busy_delay counter
        channel_busy_delay = 0

        # Initialize dbfs counter
        rms_counter = 0

        while True:
            # threading.Event().wait(0.01)
            threading.Event().wait(0.01)
            # WE NEED TO OPTIMIZE THIS!

            # Start calculating the FFT once enough samples are captured.
            if len(self.fft_data) >= 128:
                # https://gist.github.com/ZWMiller/53232427efc5088007cab6feee7c6e4c
                # Fast Fourier Transform, 10*log10(abs) is to scale it to dB
                # and make sure it's not imaginary
                try:
                    fftarray = np.fft.rfft(self.fft_data)

                    # Set value 0 to 1 to avoid division by zero
                    fftarray[fftarray == 0] = 1
                    dfft = 10.0 * np.log10(abs(fftarray))

                    # get average of dfft
                    avg = np.mean(dfft)

                    # Detect signals which are higher than the
                    # average + 10 (+10 smoothes the output).
                    # Data higher than the average must be a signal.
                    # Therefore we are setting it to 100 so it will be highlighted
                    # Have to do this when we are not transmitting so our
                    # own sending data will not affect this too much
                    if not TNC.transmitting:
                        dfft[dfft > avg + 15] = 100

                        # Calculate audio dbfs
                        # https://stackoverflow.com/a/9763652
                        # calculate dbfs every 50 cycles for reducing CPU load
                        rms_counter += 1
                        if rms_counter > 50:
                            d = np.frombuffer(self.fft_data, np.int16).astype(np.float32)
                            # calculate RMS and then dBFS
                            # https://dsp.stackexchange.com/questions/8785/how-to-compute-dbfs
                            # try except for avoiding runtime errors by division/0
                            try:
                                rms = int(np.sqrt(np.max(d ** 2)))
                                if rms == 0:
                                    raise ZeroDivisionError
                                AudioParam.audio_dbfs = 20 * np.log10(rms / 32768)
                            except Exception as e:
                                # FIXME: Disabled for cli cleanup
                                #self.log.warning(
                                #    "[MDM] fft calculation error - please check your audio setup",
                                #    e=e,
                                #)
                                AudioParam.audio_dbfs = -100

                            rms_counter = 0

                    # Convert data to int to decrease size
                    dfft = dfft.astype(int)

                    # Create list of dfft for later pushing to AudioParam.fft
                    dfftlist = dfft.tolist()

                    # Reduce area where the busy detection is enabled
                    # We want to have this in correlation with mode bandwidth
                    # TODO: This is not correctly and needs to be checked for correct maths
                    # dfftlist[0:1] = 10,15Hz
                    # Bandwidth[Hz] / 10,15
                    # narrowband = 563Hz = 56
                    # wideband = 1700Hz = 167
                    # 1500Hz = 148
                    # 2700Hz = 266
                    # 3200Hz = 315

                    # slot
                    slot = 0
                    slot1 = [0, 65]
                    slot2 = [65,120]
                    slot3 = [120, 176]
                    slot4 = [176, 231]
                    slot5 = [231, len(dfftlist)]
                    for range in [slot1, slot2, slot3, slot4, slot5]:

                        range_start = range[0]
                        range_end = range[1]
                        # define the area, we are detecting busy state
                        #dfft = dfft[120:176] if TNC.low_bandwidth_mode else dfft[65:231]
                        slotdfft = dfft[range_start:range_end]
                        # Check for signals higher than average by checking for "100"
                        # If we have a signal, increment our channel_busy delay counter
                        # so we have a smoother state toggle
                        if np.sum(slotdfft[slotdfft > avg + 15]) >= 200 and not TNC.transmitting:
                            ModemParam.channel_busy = True
                            ModemParam.channel_busy_slot[slot] = True
                            # Limit delay counter to a maximum of 200. The higher this value,
                            # the longer we will wait until releasing state
                            channel_busy_delay = min(channel_busy_delay + 10, 200)
                        else:
                            # Decrement channel busy counter if no signal has been detected.
                            channel_busy_delay = max(channel_busy_delay - 1, 0)
                            # When our channel busy counter reaches 0, toggle state to False
                            if channel_busy_delay == 0:
                                ModemParam.channel_busy = False
                                ModemParam.channel_busy_slot[slot] = False

                        # increment slot
                        slot += 1

                    AudioParam.fft = dfftlist[:315]  # 315 --> bandwidth 3200
                except Exception as err:
                    self.log.error(f"[MDM] calculate_fft: Exception: {err}")
                    self.log.debug("[MDM] Setting fft=0")
                    # else 0
                    AudioParam.fft = [0]

    def set_frames_per_burst(self, frames_per_burst: int) -> None:
        """
        Configure codec2 to send the configured number of frames per burst.

        :param frames_per_burst: Number of frames per burst requested
        :type frames_per_burst: int
        """
        # Limit frames per burst to acceptable values
        frames_per_burst = min(frames_per_burst, 1)
        frames_per_burst = max(frames_per_burst, 5)

        frames_per_burst = 1

        codec2.api.freedv_set_frames_per_burst(self.dat0_datac1_freedv, frames_per_burst)
        codec2.api.freedv_set_frames_per_burst(self.dat0_datac3_freedv, frames_per_burst)
        codec2.api.freedv_set_frames_per_burst(self.dat0_datac4_freedv, frames_per_burst)
        codec2.api.freedv_set_frames_per_burst(self.fsk_ldpc_freedv_0, frames_per_burst)

    def reset_data_sync(self) -> None:
        """
        reset sync state for data modes

        :param frames_per_burst: Number of frames per burst requested
        :type frames_per_burst: int
        """

        codec2.api.freedv_set_sync(self.dat0_datac1_freedv, 0)
        codec2.api.freedv_set_sync(self.dat0_datac3_freedv, 0)
        codec2.api.freedv_set_sync(self.dat0_datac4_freedv, 0)
        codec2.api.freedv_set_sync(self.fsk_ldpc_freedv_0, 0)


def open_codec2_instance(mode: int) -> ctypes.c_void_p:
    """
    Return a codec2 instance of the type `mode`

    :param mode: Type of codec2 instance to return
    :type mode: Union[int, str]
    :return: C-function of the requested codec2 instance
    :rtype: ctypes.c_void_p
    """
    if mode in [codec2.FREEDV_MODE.fsk_ldpc_0.value]:
        return ctypes.cast(
            codec2.api.freedv_open_advanced(
                codec2.FREEDV_MODE.fsk_ldpc.value,
                ctypes.byref(codec2.api.FREEDV_MODE_FSK_LDPC_0_ADV),
            ),
            ctypes.c_void_p,
        )

    if mode in [codec2.FREEDV_MODE.fsk_ldpc_1.value]:
        return ctypes.cast(
            codec2.api.freedv_open_advanced(
                codec2.FREEDV_MODE.fsk_ldpc.value,
                ctypes.byref(codec2.api.FREEDV_MODE_FSK_LDPC_1_ADV),
            ),
            ctypes.c_void_p,
        )

    return ctypes.cast(codec2.api.freedv_open(mode), ctypes.c_void_p)


def get_bytes_per_frame(mode: int) -> int:
    """
    Provide bytes per frame information for accessing from data handler

    :param mode: Codec2 mode to query
    :type mode: int or str
    :return: Bytes per frame of the supplied codec2 data mode
    :rtype: int
    """
    freedv = open_codec2_instance(mode)
    # TODO: add close session
    # get number of bytes per frame for mode
    return int(codec2.api.freedv_get_bits_per_modem_frame(freedv) / 8)


def set_audio_volume(datalist, volume: float) -> np.int16:
    """
    Scale values for the provided audio samples by volume,
    `volume` is clipped to the range of 0-200

    :param datalist: Audio samples to scale
    :type datalist: NDArray[np.int16]
    :param volume: "Percentage" (0-200) to scale samples
    :type volume: float
    :return: Scaled audio samples
    :rtype: np.int16
    """
    # make sure we have float as data type to avoid crash
    try:
        volume = float(volume)
    except Exception as e:
        print(f"[MDM] changing audio volume failed with error: {e}")
        volume = 100.0

    # Clip volume provided to acceptable values
    volume = np.clip(volume, 0, 200)  # limit to max value of 255
    # Scale samples by the ratio of volume / 100.0
    data = np.fromstring(datalist, np.int16) * (volume / 100.0)  # type: ignore
    return data.astype(np.int16)


def get_modem_error_state():
    """
    get current state buffer and return True of contains 10

    """

    if RECEIVE_DATAC1 and 10 in DAT0_DATAC1_STATE:
        DAT0_DATAC1_STATE.clear()
        return True
    if RECEIVE_DATAC3 and 10 in DAT0_DATAC3_STATE:
        DAT0_DATAC3_STATE.clear()
        return True
    if RECEIVE_DATAC4 and 10 in DAT0_DATAC4_STATE:
        DAT0_DATAC4_STATE.clear()
        return True

    return False

