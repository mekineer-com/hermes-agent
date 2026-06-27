'use strict';

function browserArgs(disableServiceWorkers) {
  return [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--no-first-run',
    '--no-zygote',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-software-rasterizer',
    '--mute-audio',
    ...(disableServiceWorkers ? ['--disable-features=ServiceWorker'] : []),
  ];
}

function buildClientOptions({
  LocalAuth,
  clientId,
  authPath,
  userAgent,
  headless,
  executablePath,
  disableServiceWorkers,
}) {
  return {
    authStrategy: new LocalAuth({ clientId, dataPath: authPath }),
    ...(userAgent ? { userAgent } : {}),
    puppeteer: {
      headless,
      ...(executablePath ? { executablePath } : {}),
      args: browserArgs(disableServiceWorkers),
    },
  };
}

module.exports = {
  browserArgs,
  buildClientOptions,
};
