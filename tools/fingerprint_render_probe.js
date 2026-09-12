/* Cross-path operations with independent, opaque pixel expectations. */
globalThis.chromixRenderProbe = async () => {
  const errors = [], unavailable = [], observed = {};
  const require = (ok, why) => { if (!ok) throw Error(why); };
  const timeout = async (promise, ms = 15000) => {
    let timer;
    try { return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error('render operation timed out')), ms);
    })]); } finally { clearTimeout(timer); }
  };
  const sample = async bitmap => {
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height), ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    return Array.from(ctx.getImageData(0, 0, canvas.width, canvas.height).data);
  };
  const solid = (pixels, color) => pixels.length > 0 && pixels.every((v, i) => v === color[i % 4]);
  const capture = async (name, fn) => { try { observed[name] = await timeout(fn()); }
    catch (e) { errors.push({name, error: e.name, message: e.message}); } };
  const make = () => new OffscreenCanvas(16, 16);
  await capture('bitmap', async () => {
    const source = make(), ctx = source.getContext('2d');
    ctx.fillStyle = '#ff0000'; ctx.fillRect(0, 0, 16, 8);
    ctx.fillStyle = '#0000ff'; ctx.fillRect(0, 8, 16, 8);
    const crop = await createImageBitmap(source, 1, 1, 4, 4,
      {resizeWidth: 8, resizeHeight: 8, resizeQuality: 'pixelated'});
    const flipped = await createImageBitmap(source, {imageOrientation: 'flipY'});
    try {
      const cropPixels = await sample(crop), flippedPixels = await sample(flipped);
      require(crop.width === 8 && crop.height === 8 && solid(cropPixels, [255,0,0,255]), 'crop/resize mismatch');
      require(solid(flippedPixels.slice(0, 16 * 8 * 4), [0,0,255,255]) &&
        solid(flippedPixels.slice(16 * 8 * 4), [255,0,0,255]), 'bitmap flip mismatch');
      const destination = make(), renderer = destination.getContext('bitmaprenderer');
      require(renderer, 'bitmaprenderer unavailable');
      const transfer = await createImageBitmap(source);
      renderer.transferFromImageBitmap(transfer);
      require(transfer.width === 0 && transfer.height === 0, 'bitmaprenderer did not consume ownership');
      const result = await createImageBitmap(destination);
      try {
        const pixels = await sample(result);
        require(solid(pixels.slice(0, 16 * 8 * 4), [255,0,0,255]) &&
          solid(pixels.slice(16 * 8 * 4), [0,0,255,255]), 'bitmaprenderer pixels disagree');
      } finally { result.close(); }
      return {crop: true, resize: true, flip: true, bitmaprenderer: true, ownership: true};
    } finally { crop.close(); flipped.close(); }
  });
  await capture('exports', async () => {
    const results = [];
    for (const kind of ['html', 'offscreen']) {
      const canvas = kind === 'html' ? Object.assign(document.createElement('canvas'), {width:16,height:16}) : make();
      const ctx = canvas.getContext('2d');
      const encode = () => canvas.convertToBlob ? canvas.convertToBlob({type:'image/png'}) :
        new Promise((resolve, reject) => canvas.toBlob(b => b ? resolve(b) : reject(Error('null PNG'))));
      ctx.fillStyle = '#ff0000'; ctx.fillRect(0, 0, 16, 16); const first = encode(), same = encode();
      ctx.fillStyle = '#0000ff'; ctx.fillRect(0, 0, 16, 16); const last = encode();
      const blobs = await Promise.all([first, same, last]);
      for (let i = 0; i < blobs.length; i++) {
        const image = await createImageBitmap(blobs[i]);
        try { require(solid(await sample(image), i < 2 ? [255,0,0,255] : [0,0,255,255]),
          kind + ': export did not retain its call-time snapshot'); }
        finally { image.close(); }
      }
      results.push({kind, snapshots: 3, sourceMutation: true});
    }
    return results;
  });
  await capture('workerOwnership', async () => {
    const canvas = Object.assign(document.createElement('canvas'), {width:16,height:16});
    const offscreen = canvas.transferControlToOffscreen();
    const url = URL.createObjectURL(new Blob([`onmessage=e=>{try {
      const c=e.data,ctx=c.getContext('2d');ctx.fillStyle='#008000';ctx.fillRect(0,0,16,16);
      const image=c.transferToImageBitmap();postMessage({image},[image]);
      }catch(error){postMessage({error:String(error)})}};`], {type:'text/javascript'}));
    const worker = new Worker(url);
    try {
      const reply = new Promise((resolve,reject)=>{worker.onmessage=e=>e.data.error?reject(Error(e.data.error)):resolve(e.data.image);
        worker.onerror=e=>reject(Error(e.message));worker.postMessage(offscreen,[offscreen]);});
      let detached = false;
      try { offscreen.getContext('2d'); } catch (e) { detached = e.name === 'InvalidStateError'; }
      require(detached, 'transferred OffscreenCanvas retained sender access');
      const image = await timeout(reply);
      try { require(solid(await sample(image), [0,128,0,255]), 'worker bitmap differs from rendered pixels'); }
      finally { image.close(); }
      return {detached: true, pixels: true};
    } finally { worker.terminate(); URL.revokeObjectURL(url); }
  });
  for (const api of ['webgl', 'webgl2']) await capture(api, async () => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 2;
    const gl = canvas.getContext(api, {preserveDrawingBuffer:true});
    if (!gl) { unavailable.push(api); return {status:'unavailable'}; }
    const read = () => {
      gl.clearColor(0.25,0.5,0.75,1);gl.clear(gl.COLOR_BUFFER_BIT);
      const pixels = new Uint8Array(16);gl.readPixels(0,0,2,2,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
      require(gl.getError() === gl.NO_ERROR, 'native readback error');
      require(pixels.every((v,i)=>Math.abs(v-[64,128,191,255][i%4])<=1), 'clear/readback mismatch');
      return Array.from(pixels);
    };
    const before = read(), extension = gl.getExtension('WEBGL_lose_context');
    if (!extension) { unavailable.push(api + ':context-loss'); return {status:'partial', before}; }
    const lost = new Promise(resolve=>canvas.addEventListener('webglcontextlost', e=>{e.preventDefault();resolve();},{once:true}));
    extension.loseContext(); await timeout(lost);
    require(gl.isContextLost(), 'context loss state mismatch');
    // Default prevention is committed after loss-event dispatch. Restoring in
    // its promise microtask races that transition; resume on the next task.
    await new Promise(resolve => setTimeout(resolve, 0));
    const restored = new Promise(resolve=>canvas.addEventListener('webglcontextrestored',resolve,{once:true}));
    extension.restoreContext(); await timeout(restored);
    const after = read(); require(!gl.isContextLost(), 'context did not restore');
    return {status:'observed', before, after, contextRestored:true};
  });
  await capture('webgpu', async () => {
    if (!navigator.gpu) { unavailable.push('webgpu'); return {status:'unavailable'}; }
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) { unavailable.push('webgpu-adapter'); return {status:'unavailable'}; }
    const features = [...adapter.features].sort();
    const limits = Object.fromEntries(Object.getOwnPropertyNames(Object.getPrototypeOf(adapter.limits))
      .filter(k=>k!=='constructor' && typeof adapter.limits[k] === 'number').map(k=>[k,adapter.limits[k]]));
    const device = await adapter.requestDevice({requiredFeatures:features});
    const enabledFeatures = [...device.features].sort();
    try { require(features.every(f=>device.features.has(f)), 'advertised feature could not be requested'); }
    finally { device.destroy(); }
    const boundaries = [];
    for (const [name,value] of Object.entries(limits)) {
      if (!Number.isSafeInteger(value) || value >= Number.MAX_SAFE_INTEGER) {unavailable.push('limit:'+name);continue;}
      const invalid = name.startsWith('min') ? (value > 1 ? value / 2 : 0) : value + 1;
      const fresh = await navigator.gpu.requestAdapter();
      require(fresh, 'adapter became unavailable');
      let rejected = false;
      try { const unexpected = await fresh.requestDevice({requiredLimits:{[name]:invalid}});unexpected.destroy(); }
      catch (e) { rejected = e.name === 'OperationError'; }
      require(rejected, 'reported adapter boundary was not enforced: ' + name);
      const validAdapter = await navigator.gpu.requestAdapter();
      require(validAdapter, 'adapter unavailable for valid-boundary request');
      const validDevice = await validAdapter.requestDevice({requiredLimits:{[name]:value}});
      let accepted;
      try {
        accepted = validDevice.limits[name];
        require(name.startsWith('min') ? accepted <= value : accepted >= value,
          'valid adapter boundary was not honored: ' + name);
      } finally { validDevice.destroy(); }
      boundaries.push({name, advertised:value, requested:invalid, rejected, accepted});
    }
    return {status:'observed', features, enabledFeatures, limits, boundaries};
  });
  return {schema_version:1, observed, errors, unavailable};
};
