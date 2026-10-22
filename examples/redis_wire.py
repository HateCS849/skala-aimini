"""Small RESP2 client/gate for a dedicated local Redis; no production Redis controls."""
import asyncio


async def read(reader):
    line = await reader.readline()
    if not line:
        raise ConnectionError("Redis connection closed")
    kind, value = line[:1], line[1:-2]
    if kind == b"+":
        return value
    if kind == b"-":
        raise ConnectionError("Redis error")
    if kind == b":":
        return int(value)
    if kind == b"$":
        count = int(value)
        if count == -1:
            return None
        if not 0 <= count <= 1024 * 1024:
            raise ValueError("Redis value too large")
        return (await reader.readexactly(count + 2))[:-2]
    if kind == b"*":
        count = int(value)
        if not 0 <= count <= 20:
            raise ValueError("Redis command too large")
        return [await read(reader) for _ in range(count)]
    raise ValueError("Invalid RESP")


def encode(value):
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, int):
        return f":{value}\r\n".encode()
    if isinstance(value, list):
        return f"*{len(value)}\r\n".encode() + b"".join(encode(item) for item in value)
    value = value if isinstance(value, bytes) else str(value).encode()
    return f"${len(value)}\r\n".encode() + value + b"\r\n"


def encode_command(parts):
    # Redis command arguments must be bulk strings, including numeric EX/TTL values.
    return encode([str(value) if isinstance(value, int) else value for value in parts])


async def command(port, *parts):
    async with asyncio.timeout(.5):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(encode_command(parts))
            await writer.drain()
            return await read(reader)
        finally:
            writer.close()
            await writer.wait_closed()


async def gate(reader, writer, services, redis_port):
    try:
        async with asyncio.timeout(1):
            parts = await read(reader)
            if not isinstance(parts, list) or len(parts) < 2:
                return
            key = parts[1].decode()
            if any(key.startswith(f"burstguard:{variant}:") and state.config.cache_unavailable for variant, state in services.items()):
                return  # An actual closed TCP connection, not a fabricated cache metric.
            if parts[0].upper() not in {b"GET", b"SET"} or not key.startswith("burstguard:"):
                return
            result = await command(redis_port, *parts)
            writer.write(encode(result))
            await writer.drain()
    except (OSError, ConnectionError, TimeoutError, ValueError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
