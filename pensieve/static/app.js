// Global bootstrap only. Feature scripts live beside their templates.
document.addEventListener('DOMContentLoaded', () => {
  const theme = localStorage.getItem('pensieve.theme');
  if (theme) document.documentElement.dataset.theme = theme;
});
