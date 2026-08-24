export type TranscriptionSegment = {
  start: number;
  end: number;
  text: string;
  speaker?: string;
  confidence?: number;
};

export type TranscriptionResult = {
  language?: string;
  text: string;
  segments: TranscriptionSegment[];
};

export interface TranscriptionProvider {
  transcribe(audioPath: string): Promise<TranscriptionResult>;
}
