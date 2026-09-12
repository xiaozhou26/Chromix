import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { geoipHttp } from '../_network.js';

const data = { status: 'success', timezone: 'Asia/Tokyo', countryCode: 'JP', query: '203.0.113.9' };

function reader(socket) {
  const iterator = socket[Symbol.asyncIterator]();
  let buffered = Buffer.alloc(0);
  const read = async (size) => {
    while (buffered.length < size) {
      const { value, done } = await iterator.next();
      if (done) throw new Error('truncated test peer input');
      buffered = Buffer.concat([buffered, value]);
    }
    const value = buffered.subarray(0, size); buffered = buffered.subarray(size); return value;
  };
  return { read, until: async (marker) => {
    let value = Buffer.alloc(0);
    while (!value.subarray(-marker.length).equals(marker)) {
      value = Buffer.concat([value, await read(1)]);
      assert.ok(value.length < 8192);
    }
    return value;
  } };
}

async function peer(t, { version = 5, auth = false, failure, boundType = 1 } = {}) {
  const seen = [], errors = [], sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket)); socket.on('error', () => {});
    const { read, until } = reader(socket);
    const send = (bytes) => { for (const value of bytes) socket.write(Buffer.from([value])); };
    (async () => {
      if (failure === 'timeout') return;
      if (version === 5) {
        assert.deepEqual(await read(3), Buffer.from([5, 1, auth ? 2 : 0]));
        if (failure === 'method') { socket.end(Buffer.from([5, 255])); return; }
        send(Buffer.from([5, auth ? 2 : 0]));
        if (auth) {
          assert.equal((await read(1))[0], 1);
          const user = await read((await read(1))[0]), password = await read((await read(1))[0]);
          seen.push(['auth', user.toString(), password.toString()]);
          send(Buffer.from([1, failure === 'auth' ? 1 : 0]));
          if (failure === 'auth') return;
        }
        const header = await read(4); assert.deepEqual(header.subarray(0, 3), Buffer.from([5, 1, 0]));
        const length = header[3] === 3 ? (await read(1))[0] : (header[3] === 1 ? 4 : 16);
        const address = await read(length), port = (await read(2)).readUInt16BE();
        seen.push(['destination', header[3], address, port]);
        if (failure === 'refuse') { socket.end(Buffer.from([5, 5, 0, 1])); return; }
        if (failure === 'truncated') { socket.end(Buffer.from([5, 0])); return; }
        const bound = boundType === 3 ? Buffer.from('\x05proxy') : Buffer.alloc(boundType === 1 ? 4 : 16);
        send(Buffer.concat([Buffer.from([5, 0, 0, boundType]), bound, Buffer.alloc(2)]));
      } else {
        const header = await read(8); assert.deepEqual(header.subarray(0, 2), Buffer.from([4, 1]));
        const user = (await until(Buffer.from([0]))).subarray(0, -1);
        const address = header.subarray(4).equals(Buffer.from([0, 0, 0, 1]))
          ? (await until(Buffer.from([0]))).subarray(0, -1) : header.subarray(4);
        seen.push(['v4', address, user.toString()]); send(Buffer.from([0, 90, 0, 0, 0, 0, 0, 0]));
      }
      seen.push(['http', (await until(Buffer.from('\r\n\r\n'))).toString()]);
      const body = JSON.stringify(data);
      socket.end(`HTTP/1.1 200 OK\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`);
    })().catch((error) => { if (!failure) errors.push(error); socket.destroy(); });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve)); assert.deepEqual(errors, []);
  });
  return { port: server.address().port, seen };
}

for (const scheme of ['socks', 'socks5', 'socks5h']) for (const auth of [false, true]) for (const boundType of [1, 3, 4]) {
  test(`${scheme} ${auth ? 'credentials' : 'anonymous'} bound address ${boundType}`, async (t) => {
    const { port, seen } = await peer(t, { auth, boundType });
    const result = await geoipHttp(`${scheme}://${auth ? 'u%40:p%3A@' : ''}127.0.0.1:${port}`);
    assert.equal(result.exitIp, data.query);
    assert.deepEqual(seen.find((row) => row[0] === 'destination'), ['destination', 3, Buffer.from('ip-api.com'), 80]);
    if (auth) assert.deepEqual(seen.find((row) => row[0] === 'auth'), ['auth', 'u@', 'p:']);
    const request = seen.find((row) => row[0] === 'http')[1];
    assert.match(request, /^GET \/json\/\?fields=/); assert.match(request, /Host: ip-api.com\r\n/i);
    assert.doesNotMatch(request, /Proxy-Authorization/i);
  });
}

for (const scheme of ['socks4', 'socks4a']) {
  test(`${scheme} destination encoding`, async (t) => {
    const { port, seen } = await peer(t, { version: 4 });
    const result = await geoipHttp(`${scheme}://user@127.0.0.1:${port}`, 'http://127.0.0.2/json');
    assert.equal(result.exitIp, data.query);
    assert.deepEqual(seen[0], ['v4', scheme === 'socks4a' ? Buffer.from('127.0.0.2') : Buffer.from([127, 0, 0, 2]), 'user']);
  });
}

for (const [target, kind, address] of [
  ['http://192.0.2.1/json', 1, Buffer.from([192, 0, 2, 1])],
  ['http://[2001:db8::1]/json', 4, Buffer.from('20010db8000000000000000000000001', 'hex')],
]) {
  test(`SOCKS5 literal type ${kind}`, async (t) => {
    const { port, seen } = await peer(t);
    assert.equal((await geoipHttp(`socks5://127.0.0.1:${port}`, target)).exitIp, data.query);
    assert.deepEqual(seen[0], ['destination', kind, address, 80]);
  });
}

for (const failure of ['method', 'refuse', 'truncated', 'auth', 'timeout']) {
  test(`SOCKS failure ${failure}: one connection and no direct fallback`, async (t) => {
    const old = process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS;
    process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS = '0.1';
    t.after(() => { if (old === undefined) delete process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS; else process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS = old; });
    const { port } = await peer(t, { auth: failure === 'auth', failure });
    const original = net.createConnection; let calls = 0;
    t.mock.method(net, 'createConnection', function (options, ...args) {
      assert.equal(options.host, '127.0.0.1'); calls++;
      return original.call(this, options, ...args);
    });
    await assert.rejects(geoipHttp(`socks5://${failure === 'auth' ? 'u:p@' : ''}127.0.0.1:${port}`), /GeoIP/);
    assert.equal(calls, 1);
  });
}
