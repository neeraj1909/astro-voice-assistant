export async function endCallSafely(call, { setEnding, setStatus, onEnded, onError }) {
  setEnding(true);
  setStatus('ending');
  try {
    await call.end();
    setEnding(false);
    onEnded();
    setStatus('ready');
    return true;
  } catch (error) {
    setEnding(false);
    setStatus('error');
    onError(error);
    return false;
  }
}
