#!/usr/bin/env python3
import re
import threading
import time
from datetime import datetime
from queue import Queue
from typing import Optional

import serial
from flipper.app import App
from flipper.storage import FlipperStorage
from flipper.utils.cdc import resolve_port


class SerialMonitor:
    def __init__(self, port, baudrate=230400):
        self.port = port
        self.baudrate = baudrate
        self.output = []
        self.running = False
        self.serial = None
        self.thread = None
        self.queue = Queue()

    def start(self):
        try:
            self.serial = serial.Serial(self.port, self.baudrate, timeout=1)
            self.running = True
            self.thread = threading.Thread(target=self._read_serial)
            self.thread.daemon = True
            self.thread.start()
        except serial.SerialException as e:
            raise RuntimeError(f"Failed to open serial port {self.port}: {e}")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
        if self.serial:
            self.serial.close()

    def _read_serial(self):
        while self.running:
            try:
                if self.serial.in_waiting:
                    line = self.serial.readline().decode("utf-8", errors="replace")
                    if line:
                        line = re.sub(r"[\x00-\x1F\x7F-\x9F]", "", line)
                        datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")
                        line = f"{datetime_str} {line} \n"
                        self.output.append(line)
                        self.queue.put(line)
            except Exception as e:
                self.queue.put(f"Error reading serial: {e}")
                break

    def get_output(self):
        return "".join(self.output)


class Main(App):
    def __init__(self, no_exit=False):
        super().__init__(no_exit)
        self.test_results = None

    def init(self):
        self.parser.add_argument("-p", "--port", help="CDC Port", default="auto")
        self.parser.add_argument(
            "-t", "--timeout", help="Timeout in seconds", type=int, default=10
        )
        self.parser.add_argument(
            "-s", "--stm-port", help="Additional STM32 Serial Port", default=None
        )

        self.subparsers = self.parser.add_subparsers(help="sub-command help")

        self.parser_await_flipper = self.subparsers.add_parser(
            "await_flipper", help="Wait for Flipper to connect or reconnect"
        )
        self.parser_await_flipper.set_defaults(func=self.await_flipper)

        self.parser_run_units = self.subparsers.add_parser(
            "run_units", help="Run unit tests and post result"
        )
        self.parser_run_units.set_defaults(func=self.run_units)

    def _get_flipper(self, retry_count: Optional[int] = 1):
        port = None
        self.logger.info(f"Attempting to find flipper with {retry_count} attempts.")

        for i in range(retry_count):
            self.logger.info(f"Attempt to find flipper #{i}.")

            if port := resolve_port(self.logger, self.args.port):
                self.logger.info(f"Found flipper at {port}")
                time.sleep(1)
                break

            time.sleep(1)

        if not port:
            self.logger.info(f"Failed to find flipper {port}")
            return None

        flipper = FlipperStorage(port)
        flipper.start()
        return flipper

    def await_flipper(self):
        if not (flipper := self._get_flipper(retry_count=self.args.timeout)):
            return 1

        self.logger.info("Flipper started")
        flipper.stop()
        return 0

    def run_units(self):
        if not (flipper := self._get_flipper(retry_count=10)):
            return 1

        stm_monitor = None
        if self.args.stm_port:
            try:
                stm_monitor = SerialMonitor(self.args.stm_port)
                stm_monitor.start()
                self.logger.info(f"Started monitoring STM32 port: {self.args.stm_port}")
            except Exception as e:
                self.logger.error(f"Failed to start STM32 monitoring: {e}")
                flipper.stop()
                return 1

        self.logger.info("Running unit tests")
        flipper.send("unit_tests" + "\r")
        self.logger.info("Waiting for unit tests to complete")

        tests, elapsed_time, leak, status = None, None, None, None
        total = 0
        all_required_found = False

        full_output = []

        tests_pattern = re.compile(r"Failed tests: \d{0,}")
        time_pattern = re.compile(r"Consumed: \d{0,}")
        leak_pattern = re.compile(r"Leaked: \d{0,}")
        status_pattern = re.compile(r"Status: \w{3,}")

        try:
            while not all_required_found:
                try:
                    line = flipper.read.until("\r\n", cut_eol=True).decode()
                    self.logger.info(line)
                    if "command not found," in line:
                        self.logger.error(f"Command not found: {line}")
                        return 1

                    if "()" in line:
                        total += 1
                        self.logger.debug(f"Test completed: {line}")

                    if not tests:
                        tests = tests_pattern.match(line)
                    if not elapsed_time:
                        elapsed_time = time_pattern.match(line)
                    if not leak:
                        leak = leak_pattern.match(line)
                    if not status:
                        status = status_pattern.match(line)

                    pattern = re.compile(
                        r"(\[-]|\[\\]|\[\|]|\[/-]|\[[^\]]*\]|\x1b\[\d+D)"
                    )
                    line_to_append = pattern.sub("", line)
                    pattern = re.compile(r"\[3D[^\]]*")
                    line_to_append = pattern.sub("", line_to_append)
                    line_to_append = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')} {line_to_append}"

                    full_output.append(line_to_append)

                    if tests and elapsed_time and leak and status:
                        all_required_found = True
                        try:
                            remaining = flipper.read.until(">: ", cut_eol=True).decode()
                            if remaining.strip():
                                full_output.append(remaining)
                        except:
                            pass
                        break

                except Exception as e:
                    self.logger.error(f"Error reading output: {e}")
                    raise

            if None in (tests, elapsed_time, leak, status):
                raise RuntimeError(
                    f"Failed to parse output: {tests} {elapsed_time} {leak} {status}"
                )

            leak = int(re.findall(r"[- ]\d+", leak.group(0))[0])
            status = re.findall(r"\w+", status.group(0))[1]
            tests = int(re.findall(r"\d+", tests.group(0))[0])
            elapsed_time = int(re.findall(r"\d+", elapsed_time.group(0))[0])

            test_results = {
                "full_output": "\n".join(full_output),
                "total_tests": total,
                "failed_tests": tests,
                "elapsed_time_ms": elapsed_time,
                "memory_leak_bytes": leak,
                "status": status,
            }

            self.test_results = test_results

            output_file = "unit_tests_output.txt"
            with open(output_file, "w") as f:
                f.write(test_results["full_output"])

            if stm_monitor:
                test_results["stm_output"] = stm_monitor.get_output()
                stm_output_file = "unit_tests_stm_output.txt"
                with open(stm_output_file, "w") as f:
                    f.write(test_results["stm_output"])

            print(
                f"::notice:: Total tests: {total} Failed tests: {tests} Status: {status} Elapsed time: {elapsed_time / 1000} s Memory leak: {leak} bytes"
            )

            if tests > 0 or status != "PASSED":
                self.logger.error(f"Got {tests} failed tests.")
                self.logger.error(f"Leaked (not failing on this stat): {leak}")
                self.logger.error(f"Status: {status}")
                self.logger.error(f"Time: {elapsed_time / 1000} seconds")
                return 1

            self.logger.info(f"Leaked (not failing on this stat): {leak}")
            self.logger.info(
                f"Tests ran successfully! Time elapsed {elapsed_time / 1000} seconds. Passed {total} tests."
            )
            return 0

        finally:
            if stm_monitor:
                stm_monitor.stop()
            flipper.stop()


if __name__ == "__main__":
    Main()()
