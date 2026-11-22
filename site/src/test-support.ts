/** Narrow a value an assertion has already shown to exist; throws with the label if it does not. */
export function must<T>(value: T | undefined | null, label = "value"): T {
  if (value === undefined || value === null) throw new Error(`expected ${label} to be present`);
  return value;
}
