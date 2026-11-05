export class TranscriptModel {
  constructor({ onChange = () => {}, onStatus = () => {}, onError = () => {} } = {}) {
    this.onChange = onChange;
    this.onStatus = onStatus;
    this.onError = onError;
    this.messages = [];
    this.seenUserItems = new Set();
    this.seenAssistantItems = new Set();
    this.outputBuffers = new Map();
    this.staleResponses = new Set();
    this.activeResponseId = null;
    this.turn = 0;
  }

  receive(event) {
    const type = event?.type;
    if (type === 'input_audio_buffer.speech_started') {
      this.turn += 1;
      if (this.activeResponseId) {
        this.staleResponses.add(this.activeResponseId);
        this.#interruptResponse(this.activeResponseId);
        this.activeResponseId = null;
      }
      this.onStatus('listening');
      return;
    }
    if (type === 'input_audio_buffer.speech_stopped') {
      this.onStatus('thinking');
      return;
    }
    if (type === 'conversation.item.input_audio_transcription.completed') {
      const itemId = event.item_id ?? `turn-${this.turn}:${event.transcript ?? ''}`;
      if (this.seenUserItems.has(itemId) || typeof event.transcript !== 'string' || !event.transcript.trim()) return;
      this.seenUserItems.add(itemId);
      this.messages.push({ id: `user:${itemId}`, role: 'user', text: event.transcript.trim() });
      this.onChange(this.messages);
      return;
    }
    if (type === 'conversation.item.added' || type === 'conversation.item.done') {
      this.#receiveAssistantTextItem(event.item);
      return;
    }
    if (type === 'response.created') {
      const responseId = event.response?.id ?? event.response_id;
      this.activeResponseId = responseId ?? this.activeResponseId;
      this.onStatus('thinking');
      return;
    }
    if (type === 'response.output_audio_transcript.delta' || type === 'response.audio_transcript.delta') {
      this.#receiveAssistantDelta(event);
      return;
    }
    if (type === 'response.output_audio_transcript.done' || type === 'response.audio_transcript.done') {
      this.#receiveAssistantDone(event);
      return;
    }
    if (type === 'response.output_audio_buffer.started' || type === 'output_audio_buffer.started') {
      this.onStatus('speaking');
      return;
    }
    if (type === 'response.done') {
      const responseId = event.response?.id ?? event.response_id;
      if (responseId && this.staleResponses.has(responseId)) return;
      const response = event.response;
      for (const item of response?.output ?? []) {
        for (const content of item.content ?? []) {
          if (typeof content.transcript === 'string') {
            this.#receiveAssistantDone({
              response_id: responseId,
              item_id: item.id,
              transcript: content.transcript,
            });
          }
        }
      }
      if (responseId && this.activeResponseId === responseId) this.activeResponseId = null;
      this.onStatus('listening');
      return;
    }
    if (type === 'error') {
      // VAD may already have cancelled the response before the server's
      // redundant cancellation reaches the provider; the call remains usable.
      if (event.error?.code === 'response_cancel_not_active') return;
      this.onStatus('error');
      this.onError(new Error('The voice service encountered an error. End the conversation and try again.'));
    }
  }

  clear() {
    this.messages = [];
    this.seenUserItems.clear();
    this.seenAssistantItems.clear();
    this.outputBuffers.clear();
    this.staleResponses.clear();
    this.activeResponseId = null;
    this.turn = 0;
    this.onChange(this.messages);
  }

  #receiveAssistantDelta(event) {
    const responseId = event.response_id ?? this.activeResponseId;
    if (!responseId || this.staleResponses.has(responseId) || typeof event.delta !== 'string') return;
    const itemId = event.item_id ?? 'audio-transcript';
    const key = `${responseId}:${itemId}`;
    if (this.seenAssistantItems.has(key)) return;
    const text = (this.outputBuffers.get(key) ?? '') + event.delta;
    this.outputBuffers.set(key, text);
    this.#upsertAssistant(key, text, false);
    this.onStatus('speaking');
  }

  #receiveAssistantDone(event) {
    const responseId = event.response_id ?? this.activeResponseId;
    if (responseId && this.staleResponses.has(responseId)) return;
    const itemId = event.item_id ?? 'audio-transcript';
    const key = `${responseId ?? `turn-${this.turn}`}:${itemId}`;
    if (this.seenAssistantItems.has(key)) return;
    const transcript = typeof event.transcript === 'string'
      ? event.transcript
      : this.outputBuffers.get(key) ?? '';
    if (!transcript.trim()) return;
    this.seenAssistantItems.add(key);
    this.outputBuffers.delete(key);
    this.#upsertAssistant(key, transcript.trim(), true, false);
  }

  #receiveAssistantTextItem(item) {
    if (item?.type !== 'message' || item.role !== 'assistant' || !Array.isArray(item.content)) return;
    const text = item.content
      .filter((part) => part?.type === 'output_text' && typeof part.text === 'string')
      .map((part) => part.text)
      .join('')
      .trim();
    if (!text) return;
    const key = `text:${item.id ?? text}`;
    if (this.#upsertAssistant(key, text, true, true)) this.onStatus('listening');
  }

  #upsertAssistant(key, text, complete, textOnly = false) {
    const id = `assistant:${key}`;
    const existing = this.messages.find((message) => message.id === id);
    if (existing && existing.text === text && existing.complete === complete && existing.textOnly === textOnly) return false;
    if (existing) {
      existing.text = text;
      existing.complete = complete;
      existing.textOnly = textOnly;
    } else {
      this.messages.push({ id, role: 'assistant', text, complete, textOnly });
    }
    this.onChange(this.messages);
    return true;
  }

  #interruptResponse(responseId) {
    const prefix = `${responseId}:`;
    const unfinished = [...this.outputBuffers.entries()].filter(([key]) => key.startsWith(prefix));
    for (const [key, text] of unfinished) {
      if (text.trim()) {
        const id = `assistant:${key}`;
        const existing = this.messages.find((message) => message.id === id);
        if (existing) {
          existing.text = text.trim();
          existing.complete = true;
          existing.interrupted = true;
        } else {
          this.messages.push({ id, role: 'assistant', text: text.trim(), complete: true, interrupted: true });
        }
      }
      this.outputBuffers.delete(key);
      this.seenAssistantItems.add(key);
    }
    if (unfinished.length) this.onChange(this.messages);
  }
}
