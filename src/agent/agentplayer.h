#pragma once

#include <QJsonObject>
#include <QObject>
#include <QStringList>
#include <array>
#include <map>
#include <memory>

class ControlProxy;
class PlayerManager;

namespace mixxx {

class AgentPlayer {
  public:
    virtual ~AgentPlayer() = default;
    virtual bool available() const = 0;
    virtual QStringList capabilities() const = 0;
    virtual QJsonObject state() const = 0;
    virtual QString loadedPath(int deck) const = 0;
    virtual bool loading(int deck) const = 0;
    virtual bool transitionReady() const = 0;
    virtual bool setValue(int deck, const QString& key, double value) = 0;
    virtual bool setEffect(int unit, bool enabled) = 0;
    virtual void load(int deck, const QString& path) = 0;
    virtual void cancelLoads() = 0;
};

class MixxxAgentPlayer final : public QObject, public AgentPlayer {
  public:
    explicit MixxxAgentPlayer(PlayerManager* pPlayers);
    ~MixxxAgentPlayer() override;
    bool available() const override;
    QStringList capabilities() const override;
    QJsonObject state() const override;
    QString loadedPath(int deck) const override;
    bool loading(int deck) const override;
    bool transitionReady() const override;
    bool setValue(int deck, const QString& key, double value) override;
    bool setEffect(int unit, bool enabled) override;
    void load(int deck, const QString& path) override;
    void cancelLoads() override;

  private:
    ControlProxy* control(const QString& group, const QString& key) const;
    void addControl(const QString& group, const QString& key);
    PlayerManager* const m_pPlayers;
    std::map<QString, std::unique_ptr<ControlProxy>> m_controls;
    std::array<bool, 2> m_loading{false, false};
};

}
