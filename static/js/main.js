// Toggle expandable app rows
function toggleRow(header) {
  const body = header.nextElementSibling;
  const arrow = header.querySelector('span:last-child');
  if (body && body.classList.contains('app-row-body')) {
    body.classList.toggle('open');
    if (arrow) arrow.textContent = body.classList.contains('open') ? '▲' : '▼';
  }
}

// Auto-dismiss alerts after 5 seconds
document.addEventListener('DOMContentLoaded', function() {
  const alerts = document.querySelectorAll('.alert');
  alerts.forEach(function(alert) {
    setTimeout(function() {
      alert.style.transition = 'opacity 0.4s';
      alert.style.opacity = '0';
      setTimeout(function() { alert.remove(); }, 400);
    }, 5000);
  });
});
