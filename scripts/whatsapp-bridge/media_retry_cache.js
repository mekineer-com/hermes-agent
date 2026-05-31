export function buildMediaRetryCachePayload(type, { caption, fileName } = {}) {
  const safeCaption = typeof caption === 'string' && caption.trim() ? caption : undefined;
  const safeFileName = typeof fileName === 'string' && fileName.trim() ? fileName : undefined;

  if (type === 'image') {
    return { image: { caption: safeCaption } };
  }
  if (type === 'video') {
    return { video: { caption: safeCaption } };
  }
  if (type === 'audio') {
    return { audio: {} };
  }
  return { document: { fileName: safeFileName, caption: safeCaption } };
}
