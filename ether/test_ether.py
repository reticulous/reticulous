"""Fake stations on real UDP sockets, against the ether as a child process."""

import base64
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

ETHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ether.py")

FREQ = 868_100_000
BW = 125_000
SF = 9
SYNC = 0x34

# A frame long enough that begin and end are plainly separate events, short
# enough that the tests stay quick.
FRAME_US = 300_000


def radio(mode="RX", **over):
    """The radio fields shared by a state and a transmission."""
    fields = {"mode": mode, "ready_at": 0, "freq": FREQ, "bw": BW, "sf": SF,
              "cr": 5, "sync": SYNC, "hdr": "explicit", "crc": True, "pre": 8}
    fields.update(over)
    return fields


class FakeStation:
    """One station's UDP socket, its clock and its inbox."""

    def __init__(self, sid, ether_addr):
        self.sid = sid
        self.ether = ether_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.t = sid * 1_000_000      # each station's own clock, its own origin

    def close(self):
        self.sock.close()

    def send(self, msg):
        msg = dict(msg, sid=self.sid, t=self.t)
        self.sock.sendto(json.dumps(msg).encode(), self.ether)

    def hello(self):
        self.send({"type": "hello", "slots": [0]})
        return self.expect("welcome")

    def state(self, mode="RX", **over):
        self.send(dict({"type": "state", "slot": 0}, **radio(mode, **over)))

    def tx(self, fid, payload=b"hello", span_us=FRAME_US, **over):
        t0 = self.t
        self.send(dict({"type": "tx", "slot": 0, "id": fid, "t0": t0,
                        "t_pre": t0 + span_us // 10,
                        "t_hdr": t0 + span_us // 5,
                        "t_end": t0 + span_us, "power_dbm": 14,
                        "payload": base64.b64encode(payload).decode()},
                       **radio("TX", **over)))

    def recv(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return None
        return json.loads(data.decode())

    def expect(self, kind, timeout=2.0):
        """The next message of this type, ignoring anything else."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.recv(max(0.05, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("type") == kind:
                return msg
        raise AssertionError("station %d never saw %s" % (self.sid, kind))

    def expect_nothing(self, timeout=0.5):
        assert self.recv(timeout) is None


@pytest.fixture
def ether(tmp_path):
    """The ether on an ephemeral port, its record in the test's directory."""
    record = tmp_path / "record.tsv"
    proc = subprocess.Popen(
        [sys.executable, ETHER, "--bind", "127.0.0.1:0", "--record", str(record)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()

    def drain():
        for line in proc.stderr:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=drain, daemon=True).start()

    port = None
    deadline = time.monotonic() + 10
    while port is None and time.monotonic() < deadline:
        line = lines.get(timeout=10)
        if line is None:
            break
        found = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
        if found:
            port = int(found.group(1))
    assert port, "the ether never reported a listening port"

    stations = []

    def make(sid):
        station = FakeStation(sid, ("127.0.0.1", port))
        stations.append(station)
        return station

    make.record = record
    try:
        yield make
    finally:
        for station in stations:
            station.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_hello_gets_a_welcome(ether):
    welcome = ether(1).hello()
    assert welcome["mode"] == "real"
    assert isinstance(welcome["t0"], int)
    assert isinstance(welcome["seed"], int)


def test_frame_reaches_a_listening_station(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sent_at = time.monotonic()
    sender.tx(7, payload=b"over the air")

    begin = receiver.expect("rx_begin")
    assert time.monotonic() - sent_at < 0.2, "rx_begin must arrive at once"
    assert begin["id"] == 7 and begin["slot"] == 0
    assert begin["level"] == -80
    assert begin["t_end"] - begin["t0"] == FRAME_US
    assert begin["t_pre"] - begin["t0"] == FRAME_US // 10
    assert begin["t_hdr"] - begin["t0"] == FRAME_US // 5

    end = receiver.expect("rx_end")
    elapsed = time.monotonic() - sent_at
    assert FRAME_US / 1e6 - 0.05 < elapsed < FRAME_US / 1e6 + 0.3, elapsed
    assert end["id"] == 7
    assert end["verdict"] == "clean"
    assert base64.b64decode(end["payload"]) == b"over the air"
    assert end["rssi"] == -80 and end["snr"] == 10

    sender.expect_nothing()      # a transmitter never hears itself


def test_the_sender_is_not_a_receiver_and_others_must_match(ether):
    sender, same, other_freq, sleeping = ether(1), ether(2), ether(3), ether(4)
    for station in (sender, same, other_freq, sleeping):
        station.hello()
    same.state("RX")
    other_freq.state("RX", freq=869_500_000)
    sleeping.state("SLEEP")
    time.sleep(0.1)

    sender.tx(11, payload=b"x")
    assert same.expect("rx_begin")["id"] == 11
    other_freq.expect_nothing()
    sleeping.expect_nothing()


def test_wrong_sync_word_is_not_heard(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX", sync=0x12)
    time.sleep(0.1)

    sender.tx(12)
    receiver.expect_nothing()


def test_overlapping_frames_both_end_as_crc(ether):
    first, second, receiver = ether(1), ether(2), ether(3)
    for station in (first, second, receiver):
        station.hello()
    receiver.state("RX")
    time.sleep(0.1)

    first.tx(21, payload=b"first")
    receiver.expect("rx_begin")
    time.sleep(FRAME_US / 2e6)          # squarely inside the first frame
    second.tx(22, payload=b"second")
    receiver.expect("rx_begin")

    ends = {}
    for _ in range(2):
        end = receiver.expect("rx_end", timeout=2.0)
        ends[end["id"]] = end
    assert set(ends) == {21, 22}
    assert ends[21]["verdict"] == "crc"
    assert ends[22]["verdict"] == "crc"
    # The bytes of a spoiled frame are still the sender's bytes.
    assert base64.b64decode(ends[21]["payload"]) == b"first"
    assert base64.b64decode(ends[22]["payload"]) == b"second"


def test_frames_that_do_not_overlap_stay_clean(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(31, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"
    sender.tx(32, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"


def test_leaving_rx_before_a_frame_means_it_is_not_heard(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.05)
    receiver.state("STDBY_RC")
    time.sleep(0.05)

    sender.tx(41)
    receiver.expect_nothing()


def test_unknown_messages_are_ignored(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    sender.send({"type": "wait", "until": 5})
    sender.sock.sendto(b"not json at all", sender.ether)
    time.sleep(0.1)

    sender.tx(51, payload=b"still working")
    assert receiver.expect("rx_begin")["id"] == 51
    assert base64.b64decode(receiver.expect("rx_end")["payload"]) == b"still working"


def test_the_record_holds_every_message(ether):
    sender, receiver = ether(1), ether(2)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)
    sender.tx(61, payload=b"recorded")
    receiver.expect("rx_begin")
    receiver.expect("rx_end")
    time.sleep(0.2)

    lines = [l for l in ether.record.read_text().splitlines() if not l.startswith("#")]
    rows = [l.split("\t") for l in lines]
    assert all(len(r) == 4 for r in rows)
    kinds = [(r[1], r[2], json.loads(r[3])["type"]) for r in rows]
    assert ("in", "1", "hello") in kinds
    assert ("out", "1", "welcome") in kinds
    assert ("in", "2", "state") in kinds
    assert ("in", "1", "tx") in kinds
    assert ("out", "2", "rx_begin") in kinds
    assert ("out", "2", "rx_end") in kinds
