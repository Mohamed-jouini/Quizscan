// Comportements des pages de QuizScan, réunis ici et jamais écrits dans les
// pages : la politique de contenu (grader/securite.py) n'accepte que les
// scripts servis par le site. Un script ou un « onclick » glissé dans une page
// par une faille ne s'exécute donc pas, et ceux d'une page non plus : tout
// nouveau comportement s'ajoute dans ce fichier.
'use strict';
(function () {
  // Confirmation avant un geste : <button data-confirmer="Supprimer… ?">
  document.addEventListener("click", function (ev) {
    var bouton = ev.target.closest("[data-confirmer]");
    if (bouton && !window.confirm(bouton.dataset.confirmer)) ev.preventDefault();
  });

  // Page qui suit un traitement en cours : <span data-recharger="3000" hidden>
  var recharger = document.querySelector("[data-recharger]");
  if (recharger) {
    setTimeout(function () { location.reload(); }, Number(recharger.dataset.recharger));
  }

  // Trop de fichiers pour un envoi : dit tout de suite, plutôt qu'après des heures
  // de téléversement, par une page d'erreur et sans rien avoir reçu.
  document.querySelectorAll("input[data-max-fichiers]").forEach(function (champ) {
    champ.addEventListener("change", function () {
      var max = Number(champ.dataset.maxFichiers);
      var trop = champ.files.length > max;
      champ.setCustomValidity(trop
        ? champ.files.length.toLocaleString("fr-FR") + " fichiers choisis : "
          + max.toLocaleString("fr-FR") + " au plus par envoi. Envoyez le reste ensuite, "
          + "dans un second envoi."
        : "");
      if (trop) champ.reportValidity();
    });
  });

  // Tableau des questions : le bouton « enregistrer » paraît au premier changement
  function montrerBouton(ev) {
    var champ = ev.target.closest("[data-montrer-bouton]");
    var bouton = champ && champ.form && champ.form.querySelector("button");
    if (bouton) bouton.hidden = false;
  }
  document.addEventListener("change", montrerBouton);
  document.addEventListener("input", montrerBouton);

  // Registre, candidats : case « tout cocher » de l'en-tête du tableau
  document.querySelectorAll(".tout-cocher").forEach(function (maitre) {
    maitre.addEventListener("change", function () {
      maitre.closest("table").querySelectorAll('input[name="choix"]').forEach(function (c) {
        c.checked = maitre.checked;
      });
    });
  });

  // Candidats : « Supprimer les cochés » dit combien il en supprime et ne part pas
  // à vide ; « toutes pages comprises » coche aussi la page affichée.
  document.querySelectorAll("[data-compter-coches]").forEach(function (bouton) {
    var form = bouton.form;
    var tous = form.querySelector('input[name="tous"]');
    var libelle = bouton.textContent;
    function maj() {
      var n = tous && tous.checked ? Number(bouton.dataset.total)
        : form.querySelectorAll('input[name="choix"]:checked').length;
      bouton.disabled = n === 0;
      bouton.textContent = n ? libelle + " (" + n + ")" : libelle;
      bouton.dataset.confirmer = "Supprimer " + n + " candidat" + (n > 1 ? "s" : "")
        + " ? Ceux qui ont des copies corrigées sont gardés.";
    }
    form.addEventListener("change", function (ev) {
      if (tous && ev.target === tous) {
        form.querySelectorAll('input[name="choix"], .tout-cocher').forEach(function (c) {
          c.checked = tous.checked;
        });
      } else if (tous && !ev.target.checked) {
        tous.checked = false;            // une case décochée : plus « toutes »
      }
      maj();
    });
    maj();
  });

  // Œil « afficher / masquer » du champ mot de passe (pages de connexion)
  document.querySelectorAll(".reveal").forEach(function (btn) {
    var input = document.getElementById(btn.dataset.target);
    if (!input) return;
    btn.addEventListener("click", function () {
      var shown = input.type === "text";
      input.type = shown ? "password" : "text";
      btn.setAttribute("aria-pressed", String(!shown));
      btn.setAttribute("aria-label", shown ? "Afficher le mot de passe"
                                           : "Masquer le mot de passe");
      btn.querySelector(".off").hidden = shown;
      btn.querySelector(".on").hidden = !shown;
    });
  });

  // Création d'une épreuve : les champs affichés suivent le type d'épreuve.
  // QCM -> fiche et pénalité ; écrit -> barème, identification, en-tête et
  // durée du sujet ; oral -> barème seulement.
  if (document.querySelector("[data-formulaire-epreuve]")) {
    var row = function (nom) { return document.getElementById("row_" + nom); };
    var cacher = function (nom, oui) { var r = row(nom); if (r) r.hidden = oui; };
    var sheet = document.getElementById("id_sheet_mode");
    var idmode = document.getElementById("id_id_mode");
    var precSel = document.getElementById("id_epreuve_precedente");
    var type = function () {
      var c = document.querySelector('input[name="type_epreuve"]:checked');
      return c ? c.value : "qcm";
    };
    var sync = function () {
      var t = type(), qcm = t === "qcm", oral = t === "oral";
      cacher("note_sur", qcm);
      cacher("sheet_mode", !qcm);
      cacher("wrong_penalty", !qcm);
      cacher("id_mode", oral);
      cacher("anonymat", oral);
      cacher("grading_mode", oral);
      // en-tête, durée et date : imprimés sur le sujet (QCM « sujet séparé », écrit)
      var sujet = t === "ecrit" || (qcm && sheet && sheet.value === "split");
      cacher("entete", !sujet);
      cacher("duree", !sujet);
      cacher("date_epreuve", !sujet);
      cacher("id_digits", oral || !idmode || idmode.value !== "grid");
      // QCM -> écrit -> oral : un QCM ne suit aucune épreuve, un écrit suit un
      // QCM, un oral suit un écrit
      cacher("epreuve_precedente", qcm);
      if (precSel) {
        precSel.querySelectorAll("option[data-type]").forEach(function (o) {
          o.hidden = o.disabled = qcm || o.dataset.type !== (oral ? "ecrit" : "qcm");
        });
        var choisie = precSel.selectedOptions[0];
        if (choisie && choisie.disabled) precSel.value = "";
      }
      // admis d'une épreuve précédente : la liste est créée, pas de classe
      cacher("class_group", !!(precSel && precSel.value));
      var aide = document.getElementById("aide_qcm");
      if (aide) aide.hidden = !qcm;
    };
    document.querySelectorAll('input[name="type_epreuve"]').forEach(function (r) {
      r.addEventListener("change", sync);
    });
    if (sheet) sheet.addEventListener("change", sync);
    if (idmode) idmode.addEventListener("change", sync);
    if (precSel) precSel.addEventListener("change", sync);
    sync();
  } else {
    // Réglages : « Nombre de chiffres du n° » ne concerne que la grille de n°
    var mode = document.querySelector("select[name=id_mode]");
    var chiffres = document.getElementById("row_id_digits");
    if (mode && chiffres) {
      var syncChiffres = function () { chiffres.hidden = mode.value !== "grid"; };
      mode.addEventListener("change", syncChiffres);
      syncChiffres();
    }
  }

  // Ajout d'une question : les choix saisis décident du nombre de cases
  var donnees = document.querySelector("[data-lettres]");
  var ta = document.getElementById("id_choices_text");
  var sel = document.getElementById("id_correct_letter");
  if (donnees && ta && sel) {
    var letters = donnees.dataset.lettres.split(",");
    // Sans texte de choix : le nombre de cases du quiz.
    var defN = Number(donnees.dataset.cases);
    var qtype = document.getElementById("id_qtype");
    var rows = {};
    ["choices_text", "correct_letter", "open_height_mm", "points"].forEach(function (n) {
      rows[n] = document.getElementById("row_" + n);
    });
    var rebuild = function () {
      var lines = ta.value.split("\n").filter(function (l) { return l.trim() !== ""; });
      var n = Math.min(Math.max(lines.length || defN, 2), letters.length);

      // Une question manuscrite n'a ni choix ni bonne réponse ; une QCM n'a pas
      // de hauteur de zone de réponse.
      var open = qtype && qtype.value === "open";
      ["choices_text", "correct_letter"].forEach(function (name) {
        if (rows[name]) rows[name].hidden = open;
      });
      if (rows.open_height_mm) rows.open_height_mm.hidden = !open;
      if (rows.points) rows.points.hidden = !open;     // une QCM vaut 1 point

      var cur = sel.value;
      sel.innerHTML = '<option value="">— à définir plus tard —</option>';
      for (var i = 0; i < n; i++) {
        var o = document.createElement("option");
        o.value = i; o.textContent = letters[i];
        if (String(i) === cur) o.selected = true;
        sel.appendChild(o);
      }
    };
    ta.addEventListener("input", rebuild);
    if (qtype) qtype.addEventListener("change", rebuild);
    rebuild();
  }
})();
