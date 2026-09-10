"""
Fixed IVR lines the customer hears in their own language during a handover.
Pre-translated for the languages we expect; anything else is translated once
at call time by the LLM (Hermes) and cached for the process lifetime.
"""

from __future__ import annotations

PHRASES: dict[str, dict[str, str]] = {
    "connecting": {
        "en": "Please stay on the line. I'm connecting you to a colleague now. This call is important to us.",
        "de": "Bitte bleiben Sie dran. Ich verbinde Sie jetzt mit einer Kollegin oder einem Kollegen. Dieser Anruf ist uns wichtig.",
        "fr": "Merci de rester en ligne. Je vous mets en relation avec un collègue. Votre appel est important pour nous.",
        "es": "Por favor, no cuelgue. Le estoy pasando con un compañero. Su llamada es importante para nosotros.",
        "it": "Resti in linea, per favore. La sto mettendo in contatto con un collega. La sua chiamata è importante per noi.",
        "pt": "Por favor, aguarde na linha. Estou a transferir a chamada para um colega. A sua chamada é importante para nós.",
        "nl": "Blijft u alstublieft aan de lijn. Ik verbind u nu door met een collega. Uw gesprek is belangrijk voor ons.",
        "pl": "Proszę pozostać na linii. Łączę teraz z konsultantem. Ta rozmowa jest dla nas ważna.",
        "sv": "Vänligen stanna kvar på linjen. Jag kopplar dig nu till en kollega. Ditt samtal är viktigt för oss.",
        "da": "Bliv venligst på linjen. Jeg stiller dig nu om til en kollega. Dit opkald er vigtigt for os.",
        "no": "Vennligst bli på linjen. Jeg setter deg over til en kollega nå. Samtalen din er viktig for oss.",
        "fi": "Pysythän linjalla. Yhdistän sinut nyt kollegalle. Puhelusi on meille tärkeä.",
        "hi": "कृपया लाइन पर बने रहें। मैं आपको अपने सहयोगी से जोड़ रही हूँ। यह कॉल हमारे लिए महत्वपूर्ण है।",
    },
    "still_connecting": {
        "en": "Thank you for holding. A colleague will be with you very shortly.",
        "de": "Vielen Dank fürs Warten. Gleich ist jemand für Sie da.",
        "fr": "Merci de patienter. Un collègue va vous répondre dans un instant.",
        "es": "Gracias por esperar. Enseguida le atiende un compañero.",
        "it": "Grazie per l'attesa. Un collega sarà con lei a breve.",
        "pt": "Obrigado por aguardar. Um colega irá atendê-lo em breve.",
        "nl": "Bedankt voor het wachten. Een collega is zo bij u.",
        "pl": "Dziękujemy za cierpliwość. Konsultant zaraz się zgłosi.",
        "sv": "Tack för att du väntar. En kollega är strax hos dig.",
        "da": "Tak fordi du venter. En kollega er hos dig om et øjeblik.",
        "no": "Takk for at du venter. En kollega er straks hos deg.",
        "fi": "Kiitos odotuksesta. Kollega on pian linjalla.",
        "hi": "प्रतीक्षा के लिए धन्यवाद। हमारे सहयोगी कुछ ही क्षणों में आपसे बात करेंगे।",
    },
    "connected": {
        "en": "Connecting you now.",
        "de": "Ich verbinde Sie jetzt.",
        "fr": "Je vous mets en relation.",
        "es": "Le paso ahora mismo.",
        "it": "La metto in contatto adesso.",
        "pt": "A ligar agora.",
        "nl": "Ik verbind u nu door.",
        "pl": "Łączę.",
        "sv": "Jag kopplar dig nu.",
        "da": "Jeg stiller dig om nu.",
        "no": "Jeg setter deg over nå.",
        "fi": "Yhdistän nyt.",
        "hi": "अब आपको जोड़ रही हूँ।",
    },
    "callback": {
        "en": "I'm sorry, the colleague who handles this is not at their desk right now. We will call you back shortly. Thank you for your patience, and goodbye.",
        "de": "Es tut mir leid, die zuständige Person ist gerade nicht am Platz. Wir rufen Sie in Kürze zurück. Vielen Dank für Ihre Geduld und auf Wiederhören.",
        "fr": "Je suis désolée, la personne concernée n'est pas disponible pour le moment. Nous vous rappellerons très prochainement. Merci de votre patience et bonne journée.",
        "es": "Lo siento, la persona responsable no está en su puesto en este momento. Le llamaremos en breve. Gracias por su paciencia, y hasta pronto.",
        "it": "Mi dispiace, il collega responsabile non è alla sua postazione in questo momento. La richiameremo a breve. Grazie per la pazienza e arrivederci.",
        "pt": "Lamento, o colega responsável não está disponível neste momento. Iremos ligar-lhe em breve. Obrigado pela sua paciência e até breve.",
        "nl": "Het spijt me, de betreffende collega is op dit moment niet op zijn plek. Wij bellen u zo spoedig mogelijk terug. Bedankt voor uw geduld en tot ziens.",
        "pl": "Przepraszam, osoba odpowiedzialna jest w tej chwili niedostępna. Oddzwonimy wkrótce. Dziękuję za cierpliwość, do usłyszenia.",
        "sv": "Jag beklagar, den ansvariga kollegan är inte på plats just nu. Vi ringer upp dig inom kort. Tack för ditt tålamod och hej då.",
        "da": "Jeg beklager, den ansvarlige kollega er ikke ved sin plads lige nu. Vi ringer tilbage snarest. Tak for din tålmodighed og farvel.",
        "no": "Beklager, den ansvarlige kollegaen er ikke på plass akkurat nå. Vi ringer deg tilbake om kort tid. Takk for tålmodigheten, og ha det bra.",
        "fi": "Pahoittelen, vastuuhenkilö ei ole juuri nyt paikalla. Soitamme sinulle pian takaisin. Kiitos kärsivällisyydestä ja näkemiin.",
        "hi": "क्षमा करें, संबंधित अधिकारी इस समय अपनी सीट पर नहीं हैं। हम आपको शीघ्र ही वापस कॉल करेंगे। आपके धैर्य के लिए धन्यवाद, नमस्ते।",
    },
}

_cache: dict[tuple[str, str], str] = {}


def get(key: str, lang: str) -> str | None:
    lang = (lang or "en").lower()[:2]
    return PHRASES[key].get(lang)


async def get_or_translate(key: str, lang: str, llm_translate) -> str:
    """Return the phrase in `lang`; if we don't have it, translate the English
    line once with `llm_translate(text, lang) -> str` and cache it."""
    lang = (lang or "en").lower()[:2]
    hit = get(key, lang)
    if hit:
        return hit
    if (key, lang) in _cache:
        return _cache[(key, lang)]
    try:
        out = await llm_translate(PHRASES[key]["en"], lang)
        if out:
            _cache[(key, lang)] = out.strip()
            return _cache[(key, lang)]
    except Exception:  # noqa: BLE001
        pass
    return PHRASES[key]["en"]
