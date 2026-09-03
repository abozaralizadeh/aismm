/* Repeatable field rows: one thing per row, one input per value.
 *
 * Replaces the textareas that asked an operator to learn a grammar before they
 * could type two values — X communities as `ID = Name`, one per line; the Sora
 * pool as three comma-separated lists that had to line up by position — with a
 * row of plain inputs, a + to add another and an × to drop one.
 *
 * Progressive enhancement, like the rest of this dashboard: the rows are
 * ordinary inputs with ordinary names, so the form submits and the page still
 * works with scripting unavailable. Each page renders one blank row, so a
 * value can be added one save at a time even then. This file only adds the +
 * and × controls, which is exactly why the markup renders them `hidden`.
 *
 * Shared rather than per-page: two pages already needed it, and a third copy
 * is how the two would start to behave differently.
 */
(function () {
  function rowsOf(root) {
    return Array.prototype.slice.call(root.querySelectorAll('[data-repeat-row]'));
  }

  /* How few rows the list may hold. An EDITOR keeps one (there has to be
   * somewhere to type, and an × that empties the last row rather than removing
   * it reads as a broken button); a LIST of things already saved — the people a
   * connection is shared with — must be emptiable, so it sets 0. */
  function minRows(root) {
    var declared = parseInt(root.getAttribute('data-repeat-min'), 10);
    return isNaN(declared) ? 1 : declared;
  }

  function refresh(root) {
    var all = rowsOf(root);
    var floor = minRows(root);
    all.forEach(function (row) {
      var remove = row.querySelector('[data-repeat-remove]');
      if (remove) { remove.hidden = all.length <= floor; }
    });
  }

  document.querySelectorAll('[data-repeat]').forEach(function (root) {
    var list = root.querySelector('[data-repeat-rows]') || root;
    var add = root.querySelector('[data-repeat-add]');

    if (add) {
      add.hidden = false;
      add.addEventListener('click', function () {
        var all = rowsOf(root);
        if (!all.length) { return; }        // nothing to clone from
        var copy = all[all.length - 1].cloneNode(true);
        copy.querySelectorAll('input, select, textarea').forEach(function (field) {
          if (field.type === 'checkbox' || field.type === 'radio') {
            field.checked = false;
          } else if (field.tagName === 'SELECT') {
            /* Assigning '' to a select whose options have no '' value selects
             * NOTHING — a blank picker that posts an empty kind. A copy starts
             * at the first option, which is the default the page renders. */
            field.selectedIndex = 0;
          } else {
            field.value = '';
          }
          /* A placeholder that describes the ROW's state rather than the shape of
           * the value ("stored", on a key that is never shown back) is a lie on a
           * copy — the new row has nothing stored. Examples are kept. */
          if (field.hasAttribute('data-repeat-blank-placeholder')) {
            field.placeholder = '';
          }
        });
        list.appendChild(copy);
        refresh(root);
        var first = copy.querySelector('input, select, textarea');
        if (first) { first.focus(); }
      });
    }

    // Delegated, so a row added after load removes itself like any other.
    root.addEventListener('click', function (event) {
      var button = event.target.closest && event.target.closest('[data-repeat-remove]');
      if (!button || !root.contains(button)) { return; }
      var row = button.closest('[data-repeat-row]');
      if (row && rowsOf(root).length > minRows(root)) { row.remove(); }
      refresh(root);
    });

    refresh(root);
  });
})();
