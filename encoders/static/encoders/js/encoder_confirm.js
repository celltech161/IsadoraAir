document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('encoder_form');
  if (!form) return;
  form.addEventListener('submit', function (e) {
    var ok = confirm(
      'Saving this will restart the IsadoraAir encoders service, ' +
      'briefly dropping all active streams. Continue?'
    );
    if (!ok) e.preventDefault();
  });
});
