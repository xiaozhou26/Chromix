/* Owned loopback diagnostics. No navigator/prototype overrides. */
(() => {
  const require = (ok, message) => { if (!ok) throw Error(message); };
  const bounded = async (promise, ms = 10000) => {
    let timer;
    try { return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error('operation timed out')), ms);
    })]); } finally { clearTimeout(timer); }
  };
  const event = (target, name) => new Promise((resolve, reject) => {
    target.addEventListener(name, resolve, {once: true});
    target.addEventListener('error', () => reject(Error(name + ' failed')), {once: true});
  });
  const list = async () => (await navigator.mediaDevices.enumerateDevices()).map(d =>
    ({kind: d.kind, label: d.label, deviceId: d.deviceId, groupId: d.groupId}));
  const lifecycle = [];
  for (const name of ['pageshow', 'pagehide', 'freeze', 'resume', 'visibilitychange'])
    addEventListener(name, e => lifecycle.push({name, persisted: e.persisted ?? null,
      visibility: document.visibilityState, now: performance.now(), wall: Date.now()}), {capture: true});

  globalThis.chromixRuntimeProbe = {
    lifecycle,
    async geometry() {
      const box = document.createElement('div');
      box.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;pointer-events:none';
      document.documentElement.appendChild(box);
      try {
        const rect = box.getBoundingClientRect(), v = visualViewport;
        const result = {screen: {width: screen.width, height: screen.height,
          availWidth: screen.availWidth, availHeight: screen.availHeight,
          isExtended: screen.isExtended}, window: {x: screenX, y: screenY,
          innerWidth, innerHeight, outerWidth, outerHeight}, dpr: devicePixelRatio,
          layout: {width: rect.width, height: rect.height},
          viewport: v ? {width: v.width, height: v.height, scale: v.scale} : null,
          css: {width: matchMedia(`(width: ${innerWidth}px)`).matches,
            deviceWidth: matchMedia(`(device-width: ${screen.width}px)`).matches,
            deviceHeight: matchMedia(`(device-height: ${screen.height}px)`).matches,
            resolution: matchMedia(`(resolution: ${devicePixelRatio}dppx)`).matches}};
        require(Object.values(result.css).every(Boolean), 'CSS and JS geometry disagree');
        require(Math.abs(rect.width - innerWidth) <= 1 && Math.abs(rect.height - innerHeight) <= 1,
          'viewport units and actual layout disagree');
        require(screen.availWidth >= 0 && screen.availWidth <= screen.width &&
          screen.availHeight >= 0 && screen.availHeight <= screen.height, 'invalid available bounds');
        if (v) require(Math.abs(v.width * v.scale - innerWidth) <= 2, 'visual viewport scale disagrees');
        return result;
      } finally { box.remove(); }
    },
    async screens() {
      if (!globalThis.getScreenDetails) return {status: 'unavailable'};
      const details = await bounded(getScreenDetails());
      const screens = details.screens.map(s => ({left: s.left, top: s.top,
        width: s.width, height: s.height, primary: s.isPrimary, dpr: s.devicePixelRatio}));
      require(screens.filter(s => s.primary).length === 1, 'multiple or missing primary screens');
      require(screen.isExtended === (screens.length > 1), 'screen list and isExtended disagree');
      require(details.screens.includes(details.currentScreen), 'current screen absent from list');
      return {status: 'observed', screens};
    },
    async timing() {
      const samples = [];
      for (let i = 0; i < 6; i++) {
        const raf = await bounded(new Promise(requestAnimationFrame), 5000);
        samples.push({raf, now: performance.now(), wall: Date.now(), origin: performance.timeOrigin});
      }
      for (let i = 1; i < samples.length; i++) {
        require(samples[i].raf >= samples[i - 1].raf && samples[i].now >= samples[i - 1].now,
          'monotonic clock regressed');
        // Independent timer quantization may round RAF slightly ahead of now.
        require(samples[i].now + 1 >= samples[i].raf, 'RAF timestamp exceeds the quantization allowance');
      }
      const formatter = new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
      const dst = ['2024-03-10T06:59:00Z', '2024-03-10T07:00:00Z',
        '2024-11-03T05:59:00Z', '2024-11-03T06:00:00Z'].map(s => formatter.format(new Date(s)));
      require(JSON.stringify(dst) === JSON.stringify(['01:59', '03:00', '01:59', '01:00']), 'DST boundary mismatch');
      let temporal = {status: 'unavailable'};
      if (globalThis.Temporal?.Instant) {
        const z = Temporal.Instant.from('2024-03-10T07:00:00Z').toZonedDateTimeISO('America/New_York');
        require(z.hour === 3 && z.offset === '-04:00', 'Temporal and Intl disagree');
        temporal = {status: 'observed', hour: z.hour, offset: z.offset};
      }
      return {samples, dst, temporal, events: lifecycle.slice()};
    },
    async deniedMedia() {
      let name = null;
      try {
        const stream = await bounded(navigator.mediaDevices.getUserMedia({audio: true, video: true}));
        stream.getTracks().forEach(t => t.stop());
      } catch (e) { name = e.name; }
      require(name === 'NotAllowedError' || name === 'SecurityError', 'denied media unexpectedly usable');
      return {error: name, devices: await list()};
    },
    async media() {
      let stream, recorder, video, decoded, url, reader, inspectedTrack;
      try {
        const devices = await list();
        stream = await bounded(navigator.mediaDevices.getUserMedia({audio: true,
          video: {width: {ideal: 320}, height: {ideal: 240}, frameRate: {ideal: 15}}}));
        require(stream.getAudioTracks().length === 1 && stream.getVideoTracks().length === 1, 'missing capture tracks');
        const track = stream.getVideoTracks()[0];
        const capabilities = track.getCapabilities(), initial = track.getSettings();
        await bounded(track.applyConstraints({width: {ideal: 160}, height: {ideal: 120}}));
        const settings = track.getSettings();
        require(settings.width > 0 && settings.height > 0, 'invalid capture dimensions');
        // A crop-and-scale track may carry a 320x240 visible pixel rectangle
        // with a 160x120 display size. Chromium's recorder encodes visible_rect,
        // not natural_size (VideoTrackRecorderImpl::ProcessOneVideoFrame).
        // Measure the actual frame rather than inventing a dimension tolerance.
        inspectedTrack = track.clone();
        const processor = new MediaStreamTrackProcessor({track: inspectedTrack});
        reader = processor.readable.getReader();
        const {value: frame, done} = await bounded(reader.read());
        require(!done && frame, 'capture produced no VideoFrame');
        let frameInfo;
        try {
          frameInfo = {codedWidth: frame.codedWidth, codedHeight: frame.codedHeight,
            displayWidth: frame.displayWidth, displayHeight: frame.displayHeight,
            visible: frame.visibleRect.toJSON()};
          require(frame.displayWidth === settings.width && frame.displayHeight === settings.height,
            'VideoFrame display size disagrees with track settings');
        } finally { frame.close(); }
        await reader.cancel(); reader = null; inspectedTrack.stop(); inspectedTrack = null;
        video = document.createElement('video'); video.muted = true; video.srcObject = stream;
        await bounded(video.play());
        await bounded(new Promise(resolve => video.requestVideoFrameCallback((_, m) => resolve(m))));
        require(video.videoWidth === settings.width && video.videoHeight === settings.height, 'settings and decoded capture disagree');
        const mime = ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm']
          .find(t => MediaRecorder.isTypeSupported(t));
        require(mime, 'no usable recording MIME type');
        recorder = new MediaRecorder(stream, {mimeType: mime});
        const chunks = [];
        let firstChunk;
        const dataReady = new Promise(resolve => { firstChunk = resolve; });
        recorder.ondataavailable = e => { if (e.data.size) { chunks.push(e.data); firstChunk(); } };
        const stopped = event(recorder, 'stop');
        recorder.start(100);
        await bounded(dataReady);
        await new Promise(resolve => setTimeout(resolve, 500));
        recorder.stop(); await bounded(stopped);
        const blob = new Blob(chunks, {type: recorder.mimeType});
        require(blob.size > 0 && recorder.state === 'inactive', 'recording did not produce data');
        url = URL.createObjectURL(blob); decoded = document.createElement('video'); decoded.muted = true;
        const ready = event(decoded, 'loadeddata'); decoded.src = url;
        await bounded(ready);
        require(decoded.videoWidth === frameInfo.visible.width && decoded.videoHeight === frameInfo.visible.height,
          'recorded stream did not decode at its measured visible pixel dimensions: ' +
          JSON.stringify({frameInfo, decoded: [decoded.videoWidth, decoded.videoHeight]}));
        const playable = decoded.canPlayType(recorder.mimeType);
        require(playable !== '', 'successful decoder disagrees with canPlayType');
        return {devices, capabilities, initial, settings, frame: frameInfo,
          live: {width: video.videoWidth, height: video.videoHeight},
          mime: recorder.mimeType, bytes: blob.size,
          decoded: {width: decoded.videoWidth, height: decoded.videoHeight}, playable,
          mse: MediaSource.isTypeSupported(recorder.mimeType)};
      } finally {
        if (recorder?.state === 'recording') recorder.stop();
        if (reader) await reader.cancel().catch(() => {});
        inspectedTrack?.stop();
        stream?.getTracks().forEach(t => t.stop());
        if (video) { video.pause(); video.srcObject = null; }
        if (decoded) { decoded.pause(); decoded.removeAttribute('src'); decoded.load(); }
        if (url) URL.revokeObjectURL(url);
      }
    },
    async audio() {
      const context = new OfflineAudioContext(1, 4096, 44100);
      const oscillator = context.createOscillator(), compressor = context.createDynamicsCompressor();
      oscillator.frequency.value = 440; oscillator.connect(compressor); compressor.connect(context.destination);
      oscillator.start(); const rendered = await bounded(context.startRendering());
      const data = rendered.getChannelData(0);
      require(rendered.sampleRate === 44100 && data.length === 4096, 'audio graph rate/length mismatch');
      require(data.every(Number.isFinite) && data.some(v => Math.abs(v) > 0.001), 'audio graph is silent or nonfinite');
      data[0] = 0.25; const first = new Float32Array(1); rendered.copyFromChannel(first, 0);
      require(first[0] === 0.25, 'AudioBuffer read is not a mutable view');
      rendered.copyToChannel(new Float32Array([0.125]), 0);
      require(data[0] === 0.125, 'AudioBuffer copyToChannel did not update the graph buffer');
      return {sampleRate: rendered.sampleRate, frames: rendered.length, channels: rendered.numberOfChannels,
        peak: Math.max(...data.map(Math.abs)), mutable: true};
    },
  };
})();
