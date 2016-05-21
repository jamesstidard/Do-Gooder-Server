from dogooder.model.deed import Deed
from dogooder.views.deed import detailed_deed_view


def insert_deed(context: 'micro_context', description: str):
    with context.session as session:
        deed = Deed(description=description)
        session.add(deed)
        session.flush()

        result = detailed_deed_view(deed)
        context.broadcast('INSERT_DEED', result)
        return result
