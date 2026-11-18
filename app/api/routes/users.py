from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession, Paginate
from app.core.security import hash_password
from app.models import User
from app.schemas import Page, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users (admin)"])


@router.get("", response_model=Page[UserOut])
def list_users(db: DbSession, _: AdminUser, page: Paginate):
    total = db.scalar(select(func.count()).select_from(User))
    users = db.scalars(select(User).order_by(User.id).limit(page.limit).offset(page.offset))
    return Page(items=list(users), total=total, limit=page.limit, offset=page.offset)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(db: DbSession, _: AdminUser, data: UserCreate):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    user = User(
        email=email,
        full_name=data.full_name,
        role=data.role,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(db: DbSession, admin: AdminUser, user_id: int, data: UserUpdate):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes = data.model_dump(exclude_unset=True)
    if user.id == admin.id and (changes.get("is_active") is False or "role" in changes):
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't demote or disable yourself")
    if password := changes.pop("password", None):
        user.hashed_password = hash_password(password)
    for field, value in changes.items():
        setattr(user, field, value)
    db.commit()
    return user
