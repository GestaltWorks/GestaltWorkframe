export function safeHref(url: string) {
  return url.startsWith("https://") || url.startsWith("http://") ? url : "#";
}