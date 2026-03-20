"""

buf - ... acum reads
buf_readers = [..]

@(b"...")
def ...
    do something

"""

from aionetiface import *


class StreamProcessor:
    def __init__(self):
        self.buf = bytearray()
        self.buf_readers = []

    def add_reader(self, reader):
        self.buf_readers.append(reader)

    async def process(self):
        for reader in self.buf_readers:
            reader_slice = await reader(self.buf)
            if reader_slice:
                del self.buf[reader_slice]

    async def write(self, data):
        self.buf += data

stream_proc = StreamProcessor()

async def hello_reader(data):
    marker = b"hello"
    return as_slice(marker, data)

stream_proc.add_reader(hello_reader)

async def workspace():
    await stream_proc.write(b"somermsm hello sdfwer23")
    await stream_proc.process()
    print(stream_proc.buf)

async_run(workspace())

