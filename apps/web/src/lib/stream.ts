/**
 * Consumes a generation's SSE stream. EventSource resends Last-Event-ID on reconnect, and the
 * server replays from there, so a dropped connection resumes without duplicating text.
 * The client also closes on the terminal event so auto-reconnect can't re-request it.
 */
export type StreamHandlers = {
  onStart: (attempt: number) => void;
  onDelta: (text: string) => void;
  onComplete: (messageId: string, content: string) => void;
  onFailed: (code: string) => void;
};

export function followGeneration(eventsUrl: string, h: StreamHandlers): () => void {
  const es = new EventSource(eventsUrl, { withCredentials: true });
  let done = false;
  const finish = () => {
    done = true;
    es.close();
  };
  es.addEventListener("message.started", (e) => {
    // A retried attempt restarts the reply: the UI clears partial text.
    h.onStart(JSON.parse((e as MessageEvent).data).attempt);
  });
  es.addEventListener("message.delta", (e) => h.onDelta(JSON.parse((e as MessageEvent).data).text));
  es.addEventListener("message.completed", (e) => {
    const d = JSON.parse((e as MessageEvent).data);
    finish();
    h.onComplete(d.message_id, d.content);
  });
  for (const type of ["message.failed", "message.blocked"]) {
    es.addEventListener(type, (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      finish();
      h.onFailed(d.code ?? type);
    });
  }
  es.onerror = () => {
    // The browser retries automatically with Last-Event-ID; nothing to do unless we're finished.
    if (done) es.close();
  };
  return finish;
}
