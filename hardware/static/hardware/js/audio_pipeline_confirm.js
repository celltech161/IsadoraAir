document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('audiopipeline_form');
  if (!form) return;
  form.addEventListener('submit', function (e) {
    var ok = confirm(
      'Saving this will restart the IsadoraAir engine to apply the new ' +
      'sample rate. Playback will briefly interrupt. Continue?'
    );
    if (!ok) e.preventDefault();
  });
});
