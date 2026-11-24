// Bascule clair / sombre, commune a toutes les pages de QuizScan.
//
// Le reglage cycle entre trois etats : « auto » (on suit la preference du
// systeme), « clair » et « sombre ». Il est retenu dans localStorage et pose
// sur <html> comme attribut data-theme, que la feuille de style lit.
//
// Ce contrat (cle « theme », attribut data-theme, bouton .theme-toggle) est
// exactement celui de l'administration de Django : ce fichier remplace son
// admin/js/theme.js pour qu'il n'y ait qu'une implementation dans le projet,
// et le choix de l'utilisateur vaut alors pour l'application, les pages de
// connexion et l'administration indifferemment.
'use strict';
{
  const ETATS = ["auto", "light", "dark"];

  function poser(mode) {
    if (!ETATS.includes(mode)) mode = "auto";
    document.documentElement.dataset.theme = mode;
    try {
      localStorage.setItem("theme", mode);
    } catch (e) {
      // Navigation privee ou stockage refuse : le theme vaut pour la page.
    }
  }

  function lire() {
    try {
      return localStorage.getItem("theme");
    } catch (e) {
      return null;
    }
  }

  // On propose d'abord le contraire de ce que montre l'ecran : depuis
  // « auto », un clic doit changer quelque chose de visible.
  function suivant() {
    const actuel = document.documentElement.dataset.theme || "auto";
    const sombre = window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (actuel === "auto") return sombre ? "light" : "dark";
    if (actuel === (sombre ? "light" : "dark")) return sombre ? "dark" : "light";
    return "auto";
  }

  // Avant le rendu du corps : evite que la page apparaisse en clair puis
  // bascule en sombre sous les yeux de l'utilisateur.
  poser(lire() || "auto");

  document.addEventListener("click", function (ev) {
    const bouton = ev.target.closest(".theme-toggle");
    if (bouton) poser(suivant());
  });

  // Fleche « retour » apres un changement de langue (24 novembre 2026) : le navigateur
  // remet la page qu'il avait gardee (cache de navigation), dans la langue d'alors, sans la
  // redemander au serveur. Si le cookie de langue dit autre chose que la page, on la
  // recharge : le serveur la rend dans la langue choisie. Seulement au retour (page remise
  // du cache, ou navigation « back_forward ») : un rechargement est de type « reload »,
  // il ne se relance donc pas en boucle.
  window.addEventListener("pageshow", function (ev) {
    const navigation = performance.getEntriesByType ?
      performance.getEntriesByType("navigation")[0] : null;
    if (!ev.persisted && !(navigation && navigation.type === "back_forward")) return;
    const choix = document.cookie.match(/(?:^|;\s*)django_language=(fr|ar)(?:;|$)/);
    if (choix && choix[1] !== document.documentElement.lang) location.reload();
  });
}
