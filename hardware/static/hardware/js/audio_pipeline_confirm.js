document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('audiopipeline_form');
  if (!form) return;
  var restartInputs = ['id_sample_rate', 'id_program_gain_db']
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  var initialValues = restartInputs.map(function (input) { return input.value; });
  form.addEventListener('submit', function (e) {
    var restartsEngine = restartInputs.some(function (input, index) {
      return input.value !== initialValues[index];
    });
    if (!restartsEngine) return;
    var ok = confirm(
      'Saving this will restart the IsadoraAir engine to apply the new ' +
      'pipeline topology. Playback will briefly interrupt. Continue?'
    );
    if (!ok) e.preventDefault();
  });
});
