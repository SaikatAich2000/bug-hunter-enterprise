function readBrandMeta(name) {
  return document.querySelector(`meta[name="${name}"]`)?.getAttribute("content") || "";
}

export function getAppName() {
  return readBrandMeta("application-name");
}

export function getAppVersion() {
  return readBrandMeta("application-version");
}

export function getStaticAssetUrl(filename) {
  const version = readBrandMeta("application-asset-version");
  return version ? `/static/${filename}?v=${encodeURIComponent(version)}` : `/static/${filename}`;
}

/**
 * Two-tone uppercase wordmark: the first word renders plain, the rest in the
 * accent span ("Bug Hunter" → BUG + HUNTER), matching the `.wm b span` style.
 */
export function getWordmarkParts() {
  const [first = "", ...rest] = getAppName().trim().split(/\s+/);
  return {
    first: first.toUpperCase(),
    rest: rest.join(" ").toUpperCase(),
  };
}
