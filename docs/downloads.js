(() => {
  const targets = document.querySelectorAll('[data-release-downloads]');
  if (!targets.length) return;
  const api = 'https://api.github.com/repos/wandernb/omniterm/releases/latest';
  fetch(api, { headers: { Accept: 'application/vnd.github+json' } })
    .then(response => response.ok ? response.json() : Promise.reject(response))
    .then(release => {
      const assets = release.assets || [];
      const total = assets.reduce((sum, asset) => sum + asset.download_count, 0);
      const detail = assets
        .filter(asset => /\.(dmg|zip)$/i.test(asset.name))
        .map(asset => `${asset.name}: ${asset.download_count.toLocaleString()}`)
        .join(' · ');
      const text = `GitHub downloads · ${total.toLocaleString()} total${detail ? ` (${detail})` : ''}`;
      targets.forEach(target => { target.textContent = text; });
    })
    .catch(() => targets.forEach(target => {
      target.textContent = 'Download counts are available on the GitHub release page.';
    }));
})();
