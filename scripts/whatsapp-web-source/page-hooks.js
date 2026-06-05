'use strict';

async function configureResourceBlocking(page) {
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const type = request.resourceType();
    if (type === 'image' || type === 'media' || type === 'font') {
      request.abort().catch(() => {});
      return;
    }
    request.continue().catch(() => {});
  });
}

async function installRemoveMessageHook(page) {
  await page.evaluate(() => {
    if (window.__hermesWebSourceRemoveHookInstalled) return;
    const requireFn = window.require;
    const collections = typeof requireFn === 'function' ? requireFn('WAWebCollections') : null;
    const msgCollection = collections?.Msg;
    if (!msgCollection?.on) throw new Error('WAWebCollections.Msg remove hook unavailable');
    window.__hermesWebSourceRemoveHookInstalled = true;
    msgCollection.on('remove', (msg) => {
      const model = window.WWebJS?.getMessageModel ? window.WWebJS.getMessageModel(msg) : msg;
      window.__hermesWebSourceMessageRemoved(model);
    });
  });
}

module.exports = {
  configureResourceBlocking,
  installRemoveMessageHook,
};
