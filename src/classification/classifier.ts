import type {
  ClassificationInput,
  ClassificationResult,
  ClassifierBackendKind,
} from "./contracts.js";

export type ClassifierRequest = ClassificationInput;

export type ClassifierSuccess = {
  ok: true;
  backend: ClassifierBackendKind;
  result: ClassificationResult;
};

export type ClassifierFailure = {
  ok: false;
  backend: ClassifierBackendKind;
  error: string;
  retryable?: boolean;
};

export type ClassifierResponse = ClassifierSuccess | ClassifierFailure;

export interface MailClassifier {
  readonly kind: ClassifierBackendKind;
  classify(input: ClassifierRequest): Promise<ClassifierResponse>;
}
