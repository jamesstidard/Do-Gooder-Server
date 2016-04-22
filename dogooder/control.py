import datetime
from contextlib import contextmanager

from sqlalchemy import func
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm import joinedload

from blueshed.model_helpers.base_control import BaseControl

from dateutil import tz

from dogooder.utils.orm_utils import connect
from dogooder.model.user import User
from dogooder.model.deed import Deed
from dogooder.model.accomplishment import Accomplishment
from dogooder.utils.user_session import user_session


class Control(BaseControl):
    """
    Public Interface Definition and Implementation.

    Encapsulates a database connection and provides a session interface
    can generate passwords, contains a list of websocket clients and
    will broadcast state changes to interested client.
    """

    def __init__(self, db_url: str, echo: bool=False, pool_recycle=None):
        BaseControl.__init__(self, db_url, echo, pool_recycle)

        self._engine, self._Session = connect(db_url, echo, pool_recycle)
        self._clients = []
        self._pending = []

    @property
    @contextmanager
    def session(self):
        """Self closing session for use by with statements."""
        session = self._Session()

        try:
            yield session
            session.commit()
        except:
            session.rollback()
            raise
        finally:
            session.close()

    def _broadcast(self, signal: str, message: str, accl=None):
        PUBLIC = None
        news   = {"signal": signal, "message": message}

        [client.broadcast(news)
         for client in self._clients
         if accl is PUBLIC or client.current_user in accl]

    def _broadcast_on_success(self, signal: str, message: str, accl=None):
        self._pending.append((signal, message, accl))

    def _broadcast_result_(self, signal: str, message: str, accl=None):
        self._broadcast_on_success(signal, message, accl)
        return message

    def _flush(self, error=None):
        """Called by clients after completing work."""
        if not error:
            for args in self._pending:
                self._broadcast(*args)
        self._pending = []

    def _set_user(self, client, user_id):
        """Called by client on authenticating."""
        with self.session as session:
            user = session.query(User).get(user_id)
            client.user = user.to_json()

    def ping(self, client):
        """Keep-alive Endpoint."""
        return "pong"

    def sign_in(self, client, email, password):
        """Called by client to authenticate."""
        with self.session as session:
            try:
                user = session.query(User).filter(User._email == email).one()
                user.authenticate(password)
            except (NoResultFound, ValueError):
                raise Exception("Email or password incorrect")
            else:
                client.user = user.to_json()

    def sign_out(self, client):
        client.user = None

    def get_todays_deeds(self, client, timezone=None):
        with self.session as session:
            timezone    = tz.gettz(timezone)
            today       = datetime.datetime.now(tz=timezone)
            today_start = datetime.datetime(today.year, today.month, today.day)
            tomorrow    = datetime.datetime(today.year, today.month, today.day) + datetime.timedelta(days=1)
            seed        = int(today.strftime('%Y%m%d'))
            deeds       = session.query(Deed)\
                                 .filter(Deed.created < today_start)\
                                 .order_by(func.rand(seed))\
                                 .limit(2)\
                                 .all()

            if client.current_user:
                achieved = session.query(Accomplishment)\
                                  .options(joinedload(Accomplishment.deed))\
                                  .filter(Accomplishment.completed >= today_start,
                                          Accomplishment.completed < tomorrow,
                                          Accomplishment.user_id == client.current_user)\
                                  .one_or_none()

                if achieved:
                    result = session.query(Accomplishment.deed_id, func.count(Accomplishment.deed_id))\
                                    .filter(Accomplishment.completed >= today_start,
                                            Accomplishment.completed < tomorrow)\
                                    .group_by(Accomplishment.deed_id).all()

                    total_votes = sum([count for (_, count) in result])

                    for (deed_id, count) in result:
                        if achieved.deed_id is deed_id:
                            percent = 100 * float(count) / float(total_votes)
                            result  = achieved.deed.to_json()
                            result.update(percent_of_votes=percent)
                            return [result]

            return [deed.to_json() for deed in deeds]

    def get_deeds(self, _, limit=None):
        with self.session as session:
            deeds = session.query(Deed).limit(limit).all()
            return [deed.to_json() for deed in deeds]

    @user_session
    def insert_deed(self, user, session, description):
        deed = Deed(description=description)
        session.add(deed)
        session.flush()
        self._broadcast_on_success('insert_deed', deed.to_json())

    @user_session
    def accomplish_deed(self, user, session, id):
        now      = datetime.datetime.utcnow()
        today    = datetime.datetime(now.year, now.month, now.day)
        tomorrow = datetime.datetime(now.year, now.month, now.day + 1)

        todays_deeds = session.query(Accomplishment)\
                              .filter(Accomplishment.user_id == user.id,
                                      Accomplishment.completed >= today,
                                      Accomplishment.completed < tomorrow)\
                              .all()

        if len(todays_deeds) >= 1:
            raise Exception('Deed already accomplished today')

        deed           = session.query(Deed).filter(Deed.id == id).one()
        accomplishment = Accomplishment(deed=deed, user=user)

        session.add(accomplishment)
        session.flush()

        self._broadcast_on_success('update_deed', deed.to_json())
