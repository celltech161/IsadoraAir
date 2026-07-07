document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('rbdsconfig_form');
  if (!form) return;
  form.addEventListener('submit', function (e) {
    var ok = confirm(
      'Saving this will restart the IsadoraAir RBDS service to apply the ' +
      'new network/protocol settings, briefly interrupting RDS updates. Continue?'
    );
    if (!ok) e.preventDefault();
  });
});
