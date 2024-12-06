#!/usr/bin/env python3
import re
import time
import serial
import threading
from typing import Optional
from queue import Queue
from datetime import datetime

from flipper.app import App
from flipper.storage import FlipperStorage
from flipper.utils.cdc import resolve_port

TESTS_PATTERN = re.compile(r"Failed tests: \d+")
TIME_PATTERN = re.compile(r"Consumed: \d+")
LEAK_PATTERN = re.compile(r"Leaked: \d+")
STATUS_PATTERN = re.compile(r"Status: \w+")


CLEANING_PATTERN_1 = re.compile(r'(\[-]|\[\\]|\[\|]|\[/-]|\[[^\]]*\]|\x1b\[\d+D)')
CLEANING_PATTERN_2 = re.compile(r'\[3D[^\]]*')

class SerialMonitor:
    def __init__(self, port, baudrate=230400):
        self.port = port
        self.baudrate = baudrate
        self.output = []
        self.running = False
        self.serial = None
        self.thread = None
        self.queue = Queue()

    def _autodetect_port(self):
        return resolve_port(self.logger, self.args.port)

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
                    line = self.serial.readline().decode('utf-8', errors='replace')
                    if line:
                        # Inline pattern replacements for serial lines (Recommendation #9)
                        line = self._clean_line(line)
                        self.output.append(line)
                        self.queue.put(line)
            except Exception as e:
                self.queue.put(f"Error reading serial: {e}")
                break

    def _clean_line(self, line: str) -> str:
        line = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', line)
        return f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')} {line}\n"

    def get_output(self):
        return ''.join(self.output)

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

    def _initialize_flipper(self, retry_count=10):
        flipper = self._get_flipper(retry_count=retry_count)
        if not flipper:
            self.logger.error("Failed to initialize Flipper device.")
        return flipper

    def _initialize_stm_monitor(self):
        if not self.args.stm_port:
            return None
        try:
            stm_monitor = SerialMonitor(self.args.stm_port)
            stm_monitor.start()
            self.logger.info(f"Started monitoring STM32 port: {self.args.stm_port}")
            return stm_monitor
        except Exception as e:
            self.logger.error(f"Failed to start STM32 monitoring: {e}")
            return None

    def _clean_flipper_line(self, line: str) -> str:
        line_to_append = CLEANING_PATTERN_1.sub('', line)
        line_to_append = CLEANING_PATTERN_2.sub('', line_to_append)
        line_to_append = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')} {line_to_append}"
        return line_to_append

    def _parse_test_line(self, line: str):
        tests = TESTS_PATTERN.search(line)
        elapsed_time = TIME_PATTERN.search(line)
        leak = LEAK_PATTERN.search(line)
        status = STATUS_PATTERN.search(line)

        return {
            'tests': tests.group(0) if tests else None,
            'elapsed_time': elapsed_time.group(0) if elapsed_time else None,
            'leak': leak.group(0) if leak else None,
            'status': status.group(0) if status else None
        }

    def _collect_and_parse_unit_tests(self, flipper):
        tests, elapsed_time, leak, status = None, None, None, None
        total = 0
        all_required_found = False
        full_output = []

        while not all_required_found:
            line = flipper.read.until("\r\n", cut_eol=True).decode()
            self.logger.info(line)

            if 'command not found,' in line:
                self.logger.error(f"Command not found: {line}")
                return None

            if "()" in line:
                total += 1
                self.logger.debug(f"Test completed: {line}")

            parsed = self._parse_test_line(line)
            if not tests and parsed['tests']:
                tests = parsed['tests']
            if not elapsed_time and parsed['elapsed_time']:
                elapsed_time = parsed['elapsed_time']
            if not leak and parsed['leak']:
                leak = parsed['leak']
            if not status and parsed['status']:
                status = parsed['status']

            line_to_append = self._clean_flipper_line(line)
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

        if None in (tests, elapsed_time, leak, status):
            return None

        leak_val = int(re.findall(r"\d+", leak)[0])
        status_val = re.findall(r"\w+", status)[1]
        tests_val = int(re.findall(r"\d+", tests)[0])
        elapsed_time_val = int(re.findall(r"\d+", elapsed_time)[0])

        return {
            'full_output': '\n'.join(full_output),
            'total_tests': total,
            'failed_tests': tests_val,
            'elapsed_time_ms': elapsed_time_val,
            'memory_leak_bytes': leak_val,
            'status': status_val
        }

    def _save_test_results(self, test_results, stm_monitor):
        output_file = "unit_tests_output.txt"
        with open(output_file, 'w') as f:
            f.write(test_results['full_output'])

        if stm_monitor:
            test_results['stm_output'] = stm_monitor.get_output()
            stm_output_file = "unit_tests_stm_output.txt"
            with open(stm_output_file, 'w') as f:
                f.write(test_results['stm_output'])

    def _finalize_results(self, test_results):
        total = test_results['total_tests']
        tests = test_results['failed_tests']
        status = test_results['status']
        elapsed_time = test_results['elapsed_time_ms']
        leak = test_results['memory_leak_bytes']

        print(f"::notice:: Total tests: {total} Failed tests: {tests} "
              f"Status: {status} Elapsed time: {elapsed_time / 1000} s "
              f"Memory leak: {leak} bytes")

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

    def run_units(self):
        flipper = self._initialize_flipper(retry_count=10)
        if not flipper:
            return 1

        stm_monitor = self._initialize_stm_monitor()
        if self.args.stm_port and not stm_monitor:
            flipper.stop()
            return 1

        try:
            self.logger.info("Running unit tests")
            flipper.send("unit_tests" + "\r")
            self.logger.info("Waiting for unit tests to complete")
            test_results = self._collect_and_parse_unit_tests(flipper)
            if test_results is None:
                self.logger.error("Failed to run or parse unit tests.")
                return 1

            self.test_results = test_results
            self._save_test_results(test_results, stm_monitor)
            return self._finalize_results(test_results)

        finally:
            if stm_monitor:
                stm_monitor.stop()
            flipper.stop()


if __name__ == "__main__":
    Main()()
