#include "agent/agentplayer.h"

#include <QFileInfo>
#include <QJsonArray>
#include <QThread>

#include "control/controlproxy.h"
#include "effects/chains/equalizereffectchain.h"
#include "mixer/basetrackplayer.h"
#include "mixer/playermanager.h"
#include "track/track.h"

namespace mixxx {
namespace {

QString groupForDeck(int deck) {
    return QStringLiteral("[Channel%1]").arg(deck);
}

QString effectGroup(int unit) {
    return QStringLiteral("[EffectRack1_EffectUnit%1]").arg(unit);
}

QString controlId(const QString& group, const QString& key) {
    return group + QLatin1Char('/') + key;
}

const QStringList kDeckKeys{QStringLiteral("play"),
        QStringLiteral("playposition"),
        QStringLiteral("bpm"),
        QStringLiteral("volume"),
        QStringLiteral("sync_enabled"),
        QStringLiteral("track_loaded"),
        QStringLiteral("filterLow"),
        QStringLiteral("filterMid"),
        QStringLiteral("filterHigh")};

}

MixxxAgentPlayer::MixxxAgentPlayer(PlayerManager* pPlayers)
        : m_pPlayers(pPlayers) {
    for (int deck = 1; deck <= 2; ++deck) {
        const auto group = groupForDeck(deck);
        for (const auto& key : kDeckKeys) {
            addControl(group, key);
        }
        addControl(group, QStringLiteral("orientation"));
        addControl(group, QStringLiteral("main_mix"));
        addControl(group, QStringLiteral("passthrough"));
        auto* pPlayer =
                deck <= m_pPlayers->numberOfDecks() ? m_pPlayers->getDeckBase(deck - 1) : nullptr;
        if (pPlayer) {
            connect(pPlayer,
                    &BaseTrackPlayer::loadingTrack,
                    this,
                    [this, deck](TrackPointer track, TrackPointer) {
                        m_loading[deck - 1] = static_cast<bool>(track);
                    });
            connect(pPlayer, &BaseTrackPlayer::newTrackLoaded, this, [this, deck](TrackPointer) {
                m_loading[deck - 1] = false;
            });
            connect(pPlayer, &BaseTrackPlayer::playerEmpty, this, [this, deck] {
                m_loading[deck - 1] = false;
            });
        }
    }
    addControl(QStringLiteral("[Master]"), QStringLiteral("crossfader"));
    addControl(QStringLiteral("[Mixer Profile]"), QStringLiteral("xFaderReverse"));
    for (int unit = 1; unit <= 4; ++unit) {
        addControl(effectGroup(unit), QStringLiteral("enabled"));
    }
}

MixxxAgentPlayer::~MixxxAgentPlayer() = default;

void MixxxAgentPlayer::addControl(const QString& group, const QString& key) {
    QString targetGroup = group;
    QString targetKey = key;
    if (key == QStringLiteral("filterLow") || key == QStringLiteral("filterMid") ||
            key == QStringLiteral("filterHigh")) {
        targetGroup = EqualizerEffectChain::formatEffectSlotGroup(group);
        targetKey = key == QStringLiteral("filterLow")
                ? QStringLiteral("parameter1")
                : (key == QStringLiteral("filterMid") ? QStringLiteral("parameter2")
                                                      : QStringLiteral("parameter3"));
    }
    m_controls.emplace(controlId(group, key),
            std::make_unique<ControlProxy>(
                    targetGroup, targetKey, this, ControlFlag::AllowMissingOrInvalid));
}

ControlProxy* MixxxAgentPlayer::control(const QString& group, const QString& key) const {
    auto found = m_controls.find(controlId(group, key));
    return found == m_controls.end() ? nullptr : found->second.get();
}

bool MixxxAgentPlayer::available() const {
    if (m_pPlayers->numberOfDecks() < 2) {
        return false;
    }
    for (int deck = 1; deck <= 2; ++deck) {
        for (const auto& key : kDeckKeys) {
            if (!control(groupForDeck(deck), key)->valid()) {
                return false;
            }
        }
    }
    return control(QStringLiteral("[Master]"), QStringLiteral("crossfader"))->valid();
}

QStringList MixxxAgentPlayer::capabilities() const {
    if (!available()) {
        return {};
    }
    QStringList result{QStringLiteral("load_track"),
            QStringLiteral("set_playing"),
            QStringLiteral("stop"),
            QStringLiteral("seek"),
            QStringLiteral("set_bpm"),
            QStringLiteral("set_sync"),
            QStringLiteral("set_volume"),
            QStringLiteral("set_crossfader"),
            QStringLiteral("set_eq"),
            QStringLiteral("transition")};
    bool effectsAvailable = true;
    for (int unit = 1; unit <= 4; ++unit) {
        effectsAvailable &= control(effectGroup(unit), QStringLiteral("enabled"))->valid();
    }
    if (effectsAvailable) {
        result.append(QStringLiteral("set_effect"));
    }
    return result;
}

QJsonObject MixxxAgentPlayer::state() const {
    QJsonObject decks;
    for (int deck = 1; deck <= 2; ++deck) {
        const auto group = groupForDeck(deck);
        auto value = [this, &group](const QString& key) { return control(group, key)->get(); };
        decks.insert(QString::number(deck),
                QJsonObject{{QStringLiteral("playing"), value(QStringLiteral("play")) > 0},
                        {QStringLiteral("position"), value(QStringLiteral("playposition"))},
                        {QStringLiteral("bpm"), value(QStringLiteral("bpm"))},
                        {QStringLiteral("volume"), value(QStringLiteral("volume"))},
                        {QStringLiteral("sync_enabled"), value(QStringLiteral("sync_enabled")) > 0},
                        {QStringLiteral("eq"),
                                QJsonObject{
                                        {QStringLiteral("low"), value(QStringLiteral("filterLow"))},
                                        {QStringLiteral("mid"), value(QStringLiteral("filterMid"))},
                                        {QStringLiteral("high"),
                                                value(QStringLiteral("filterHigh"))}}}});
    }
    QJsonObject effects;
    for (int unit = 1; unit <= 4; ++unit) {
        const auto* pControl = control(effectGroup(unit), QStringLiteral("enabled"));
        if (pControl->valid()) {
            effects.insert(QString::number(unit), pControl->toBool());
        }
    }
    return {{QStringLiteral("decks"), decks},
            {QStringLiteral("crossfader"),
                    control(QStringLiteral("[Master]"), QStringLiteral("crossfader"))->get()},
            {QStringLiteral("effects"), effects}};
}

QString MixxxAgentPlayer::loadedPath(int deck) const {
    if (deck < 1 || deck > m_pPlayers->numberOfDecks()) {
        return {};
    }
    auto* pPlayer = m_pPlayers->getDeckBase(deck - 1);
    const auto track = pPlayer ? pPlayer->getLoadedTrack() : TrackPointer();
    return track ? QFileInfo(track->getLocation()).canonicalFilePath() : QString();
}

bool MixxxAgentPlayer::loading(int deck) const {
    const auto* loaded = control(groupForDeck(deck), QStringLiteral("track_loaded"));
    return m_loading[deck - 1] || (!loadedPath(deck).isEmpty() && !loaded->toBool());
}

bool MixxxAgentPlayer::transitionReady() const {
    for (int deck = 1; deck <= 2; ++deck) {
        const auto group = groupForDeck(deck);
        const auto* orientation = control(group, QStringLiteral("orientation"));
        const auto* mainMix = control(group, QStringLiteral("main_mix"));
        const auto* passthrough = control(group, QStringLiteral("passthrough"));
        if (!orientation->valid() || !mainMix->valid() || !passthrough->valid() ||
                orientation->get() !=
                        static_cast<double>(
                                deck == 1 ? EngineChannel::LEFT : EngineChannel::RIGHT) ||
                !mainMix->toBool() || passthrough->toBool()) {
            return false;
        }
    }
    const auto* reverse =
            control(QStringLiteral("[Mixer Profile]"), QStringLiteral("xFaderReverse"));
    return reverse->valid() && !reverse->toBool();
}

bool MixxxAgentPlayer::setValue(int deck, const QString& key, double value) {
    DEBUG_ASSERT(QThread::currentThread() == thread());
    const auto group = deck == 0 ? QStringLiteral("[Master]") : groupForDeck(deck);
    auto* pControl = control(group, key);
    if (!pControl || !pControl->valid()) {
        return false;
    }
    pControl->set(value);
    return true;
}

bool MixxxAgentPlayer::setEffect(int unit, bool enabled) {
    auto* pControl = control(effectGroup(unit), QStringLiteral("enabled"));
    if (!pControl || !pControl->valid()) {
        return false;
    }
    pControl->set(enabled ? 1 : 0);
    return true;
}

void MixxxAgentPlayer::load(int deck, const QString& path) {
    DEBUG_ASSERT(QThread::currentThread() == thread());
    m_loading[deck - 1] = true;
    m_pPlayers->slotLoadToDeck(path, deck);
}

void MixxxAgentPlayer::cancelLoads() {
    for (int deck = 1; deck <= 2; ++deck) {
        if (!m_loading[deck - 1]) {
            continue;
        }
        auto* pPlayer =
                deck <= m_pPlayers->numberOfDecks() ? m_pPlayers->getDeckBase(deck - 1) : nullptr;
        if (pPlayer) {
            pPlayer->slotLoadTrack(TrackPointer(),
#ifdef __STEM__
                    mixxx::StemChannelSelection(),
#endif
                    false);
        }
        m_loading[deck - 1] = false;
    }
}

}
