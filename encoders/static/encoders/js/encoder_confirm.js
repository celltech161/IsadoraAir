// Warns before an encoder admin submit that is likely to cause the
// encoder manager to reconcile (replace) this specific group's live
// child -- briefly interrupting streams that share ITS input device
// only, never the whole isadoraair-encoders service or any OTHER
// group (Phase 3: the manager reconciles per input-device group on
// its own, the admin no longer restarts anything -- see encoders/
// admin.py's RUNTIME_AFFECTING_FIELDS / save_model / delete_model /
// delete_queryset). This is a convenience heads-up only -- the server
// decides what actually happens, and that decision is authoritative
// regardless of what this file guesses. Mirrored here so the warning
// is accurate enough not to fire on a plain sort_order (or other
// non-runtime-affecting) edit -- if this list and the server's drift
// apart, the worst case is a wrong/missing confirmation dialog, never
// a wrong outcome.
var RUNTIME_AFFECTING_FIELDS = [
  'enabled', 'protocol', 'host', 'port', 'mount', 'username', 'password',
  'format', 'bitrate_kbps', 'input_device', 'station_name', 'genre',
  'url', 'public', 'provider', 'mp3_rate_mode'
];

// True if a form field's live value differs from what the browser
// rendered it with initially -- defaultValue/defaultChecked/
// defaultSelected are set once at page load from Django's rendered
// HTML and don't track subsequent user interaction, so comparing
// against the live value/checked state is a reliable "did this change"
// check with no extra server-side data needed.
function fieldChangedFromInitial(field) {
  if (field.type === 'checkbox' || field.type === 'radio') {
    return field.checked !== field.defaultChecked;
  }
  if (field.tagName === 'SELECT') {
    for (var i = 0; i < field.options.length; i++) {
      var opt = field.options[i];
      if (opt.selected !== opt.defaultSelected) return true;
    }
    return false;
  }
  return field.value !== field.defaultValue;
}

function anyRuntimeFieldChanged(form, nameSuffix) {
  for (var i = 0; i < RUNTIME_AFFECTING_FIELDS.length; i++) {
    var selector = '[name$="' + nameSuffix + RUNTIME_AFFECTING_FIELDS[i] + '"]';
    var fields = form.querySelectorAll(selector);
    for (var j = 0; j < fields.length; j++) {
      if (fieldChangedFromInitial(fields[j])) return true;
    }
  }
  return false;
}

document.addEventListener('DOMContentLoaded', function () {
  // Add/change form: one Encoder, name="enabled", name="host", etc.
  var form = document.getElementById('encoder_form');
  if (form) {
    var isAdd = /\/add\/?$/.test(window.location.pathname);
    form.addEventListener('submit', function (e) {
      // Adding a row is always a topology change -- unconditional,
      // matching the server (encoders/admin.py's save_model). For an
      // edit, only warn if a field the server actually restarts for
      // looks changed; a sort_order-only edit gets no dialog.
      var likelyReplace = isAdd || anyRuntimeFieldChanged(form, '');
      if (!likelyReplace) return;
      var ok = confirm(
        'Saving this will cause the encoder manager to validate and replace this ' +
        'group\'s configuration, briefly interrupting streams that share its input ' +
        'device. Other encoder groups are not affected. Continue?'
      );
      if (!ok) e.preventDefault();
    });
  }

  // Changelist with list_editable ("enabled", "sort_order"): rows are
  // formset-prefixed, e.g. name="form-0-enabled". Only the "Save"
  // button next to list_editable submits this as a bulk row-save
  // (matching request.POST's own "_save" check in Django's admin) --
  // the "Go" action button submits the same form but for a queryset
  // action instead, which isn't a runtime-affecting edit by itself and
  // shouldn't get this dialog.
  var changelistForm = document.getElementById('changelist-form');
  if (changelistForm) {
    changelistForm.addEventListener('submit', function (e) {
      var submitter = e.submitter;
      if (!submitter || submitter.name !== '_save') return;
      // Of the two list_editable fields, only "enabled" is runtime-
      // affecting -- sort_order never appears in
      // RUNTIME_AFFECTING_FIELDS, so a pure reordering save is
      // correctly silent here.
      if (!anyRuntimeFieldChanged(changelistForm, '-')) return;
      var ok = confirm(
        'Saving will cause the encoder manager to validate and replace the ' +
        'configuration for at least one changed group, briefly interrupting streams ' +
        'that share its input device. Unrelated encoder groups are not affected. Continue?'
      );
      if (!ok) e.preventDefault();
    });
  }
});
