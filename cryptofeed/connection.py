'''
Copyright (C) 2017-2021  Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''
import asyncio
import logging
from contextlib import asynccontextmanager
import time
from typing import List, Union, AsyncIterable

import aiohttp
import websockets
import requests

from cryptofeed.exceptions import ConnectionClosed

LOG = logging.getLogger('feedhandler')


class Connection:
    raw_data_callback = None

    async def read(self) -> bytes:
        raise NotImplementedError

    async def write(self, msg: str):
        raise NotImplementedError


class HTTPSync(Connection):
    def process_response(self, r, address, json=False, text=False, uuid=None):
        if self.raw_data_callback:
            self.raw_data_callback.sync_callback(r.text, time.time(), str(uuid), endpoint=address)
        r.raise_for_status()
        if json:
            return r.json()
        if text:
            return r.text
        return r

    def read(self, address: str, json=False, text=True, uuid=None):
        LOG.debug("HTTPSync: requesting data from %s", address)
        r = requests.get(address)
        return self.process_response(r, address, json=json, text=text, uuid=uuid)

    def write(self, address: str, data=None, json=False, text=True, uuid=None):
        LOG.debug("HTTPSync: post to %s", address)
        r = requests.post(address, data=data)
        return self.process_response(r, address, json=json, text=text, uuid=uuid)


class AsyncConnection(Connection):
    conn_count: int = 0

    def __init__(self, conn_id: str):
        """
        conn_id: str
            the unique identifier for the connection
        """
        AsyncConnection.conn_count += 1
        self.id: str = conn_id
        self.received: int = 0
        self.sent: int = 0
        self.last_message = None
        self.conn: Union[websockets.WebSocketClientProtocol, aiohttp.ClientSession] = None

    @property
    def uuid(self):
        return self.id

    @asynccontextmanager
    async def connect(self):
        await self._open()
        try:
            yield self
        finally:
            await self.close()

    async def _open(self):
        raise NotImplementedError

    @property
    def is_open(self) -> bool:
        raise NotImplementedError

    async def close(self):
        if self.is_open:
            conn = self.conn
            self.conn = None
            await conn.close()
            LOG.info('%s: closed connection %r', self.id, conn.__class__.__name__)


class HTTPAsyncConn(AsyncConnection):
    def __init__(self, conn_id: str):
        """
        conn_id: str
            id associated with the connection
        """
        super().__init__(f'{conn_id}.http.{self.conn_count}')

    @property
    def is_open(self) -> bool:
        return self.conn and not self.conn.closed

    async def _open(self):
        if self.is_open:
            LOG.warning('%s: HTTP session already created', self.id)
        else:
            LOG.debug('%s: create HTTP session', self.id)
            self.conn = aiohttp.ClientSession()
            self.sent = 0
            self.received = 0

    async def read(self, address: str, header=None, return_headers=False) -> bytes:
        if not self.is_open:
            await self._open()

        LOG.debug("%s: requesting data from %s", self.id, address)
        async with self.conn.get(address, headers=header) as response:
            for header in ['x-mbx-used-weight', 'x-mbx-used-weight-1m', 'X-MBX-USED-WEIGHT-1M']:
                if header in response.headers:
                    LOG.debug("%s: %s:%s", self.id, header, response.headers[header])
            data = await response.text()
            self.last_message = time.time()
            self.received += 1
            if self.raw_data_callback:
                await self.raw_data_callback(data, self.last_message, self.id, endpoint=address,
                                             header=None if return_headers is False else dict(response.headers))
            response.raise_for_status()
            if return_headers:
                return data, response.headers
            return data

    async def write(self, address: str, msg: str, header=None):
        if not self.is_open:
            await self._open()

        async with self.conn.post(address, data=msg, headers=header) as response:
            self.sent += 1
            data = await response.read()
            if self.raw_data_callback:
                await self.raw_data_callback(data, time.time(), self.id, send=address)
            response.raise_for_status()
            return data


class ThrottledHTTPAsyncConn(HTTPAsyncConn):
    def __init__(self, conn_id: str, throttle_limit=float('inf'), throttle_interval=60):
        super().__init__(conn_id)
        self.throttle_limit = throttle_limit
        self.throttle_interval = throttle_interval
        self.used_limit = 0
        self.next_reset = None

    async def read(self, address: str, header=None, return_headers=False, weight=1) -> bytes:
        self.used_limit += weight
        now = time.time()
        if not self.next_reset:
            self.next_reset = (int(now) // 60 + 1) * 60
        if self.next_reset < now:
        #if self.next_reset + self.throttle_interval < now:
            LOG.debug("Reset expired throttle")
            self.next_reset = (int(now) // 60 + 1) * 60
            self.used_limit = weight
        # else:
        #     print(self.next_reset, now, self.used_limit)

        if self.used_limit >= self.throttle_limit:
            wait_time = self.next_reset  - time.time() # + self.throttle_interval
            LOG.info("Throttling kicked in for %s, will wait %s seconds", address, wait_time)
            await asyncio.sleep(wait_time)
            self.next_reset = (int(time.time()) // 60 + 1) * 60 #time.time()
            self.used_limit = weight
        return await super().read(address, header=header, return_headers=return_headers)


class HTTPPoll(HTTPAsyncConn):
    def __init__(self, address: Union[List, str], conn_id: str, delay: float = 60, sleep: float = 1):
        super().__init__(f'{conn_id}.http.{self.conn_count}')
        if isinstance(address, str):
            address = [address]
        self.address = address

        self.sleep = sleep
        self.delay = delay

    async def read(self, header=None) -> AsyncIterable:
        while True:
            for addr in self.address:
                if not self.is_open:
                    LOG.error('%s: connection closed in read()', self.id)
                    raise ConnectionClosed
                LOG.debug("%s: polling %s", self.id, addr)
                async with self.conn.get(addr, headers=header) as response:
                    data = await response.text()
                    self.received += 1
                    self.last_message = time.time()
                    if self.raw_data_callback:
                        await self.raw_data_callback(data, self.last_message, self.id, endpoint=addr)
                    response.raise_for_status()
                    yield data
                    await asyncio.sleep(self.sleep)
            await asyncio.sleep(self.delay)


class WSAsyncConn(AsyncConnection):

    def __init__(self, address: str, conn_id: str, **kwargs):
        """
        address: str
            the websocket address to connect to
        conn_id: str
            the identifier of this connection
        kwargs:
            passed into the websocket connection.
        """
        if not address.startswith("wss://"):
            raise ValueError(f'Invalid address, must be a wss address. Provided address is: {address!r}')
        self.address = address
        super().__init__(f'{conn_id}.ws.{self.conn_count}')
        self.ws_kwargs = kwargs

    @property
    def is_open(self) -> bool:
        return self.conn and not self.conn.closed

    async def _open(self):
        if self.is_open:
            LOG.warning('%s: websocket already open', self.id)
        else:
            LOG.debug('%s: connecting to %s', self.id, self.address)
            if self.raw_data_callback:
                await self.raw_data_callback(None, time.time(), self.id, connect=self.address)
            self.conn = await websockets.connect(self.address, **self.ws_kwargs)
        self.sent = 0
        self.received = 0

    async def read(self) -> AsyncIterable:
        if not self.is_open:
            LOG.error('%s: connection closed in read()', self.id)
            raise ConnectionClosed
        if self.raw_data_callback:
            async for data in self.conn:
                self.received += 1
                self.last_message = time.time()
                await self.raw_data_callback(data, self.last_message, self.id)
                yield data
        else:
            async for data in self.conn:
                self.received += 1
                self.last_message = time.time()
                yield data

    async def write(self, data: str):
        if not self.is_open:
            LOG.error('%s: connection closed in write()', self.id)
            raise ConnectionClosed

        if self.raw_data_callback:
            await self.raw_data_callback(data, time.time(), self.id, send=self.address)
        await self.conn.send(data)
        self.sent += 1
