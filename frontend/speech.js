export function createBrowserSpeech({ windowObject = globalThis.window } = {}) {
  const Recognition = windowObject?.SpeechRecognition || windowObject?.webkitSpeechRecognition;
  const synthesis = windowObject?.speechSynthesis;
  const Utterance = windowObject?.SpeechSynthesisUtterance;

  return {
    recognitionAvailable: typeof Recognition === "function",
    synthesisAvailable: Boolean(synthesis && typeof Utterance === "function"),
    createRecognition(onUpdate, onState) {
      if (typeof Recognition !== "function") return null;
      const recognition = new Recognition();
      recognition.lang = windowObject.navigator?.language || "en-US";
      recognition.continuous = false;
      recognition.interimResults = true;
      recognition.onstart = () => onState("listening");
      recognition.onresult = (event) => {
        let transcript = "";
        for (let i = 0; i < event.results.length; i += 1) transcript += event.results[i][0].transcript;
        onUpdate(transcript, Boolean(event.results[event.results.length - 1]?.isFinal));
      };
      recognition.onerror = (event) => {
        const message = event.error === "not-allowed" || event.error === "service-not-allowed"
          ? "Microphone permission was denied. You can still type your message."
          : event.error === "no-speech"
            ? "No speech was detected. Try again or type your message."
            : `Speech input stopped (${event.error || "unknown error"}). You can type instead.`;
        onState("error", message);
      };
      recognition.onend = () => onState("idle");
      return recognition;
    },
    speak(text, onEnd, onError) {
      if (!synthesis || typeof Utterance !== "function") return false;
      synthesis.cancel();
      const utterance = new Utterance(text);
      utterance.onend = onEnd;
      utterance.onerror = (event) => onError?.(event.error || "Speech playback failed.");
      synthesis.speak(utterance);
      return true;
    },
    stopSpeaking() { synthesis?.cancel(); },
  };
}
