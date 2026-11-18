from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, CurrentUser, DbSession, Paginate
from app.models import Customer, Order
from app.schemas import CustomerCreate, CustomerOut, CustomerUpdate, Page

router = APIRouter(prefix="/customers", tags=["customers"])


def _get(db, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    return customer


@router.get("", response_model=Page[CustomerOut])
def list_customers(db: DbSession, _: CurrentUser, page: Paginate, q: str | None = None):
    """List customers. `q` searches name, email and phone."""
    query = select(Customer)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Customer.name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like))
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Customer.name).limit(page.limit).offset(page.offset))
    return Page(items=list(items), total=total, limit=page.limit, offset=page.offset)


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(db: DbSession, _: CurrentUser, data: CustomerCreate):
    customer = Customer(**data.model_dump())
    db.add(customer)
    db.commit()
    return customer


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(db: DbSession, _: CurrentUser, customer_id: int):
    return _get(db, customer_id)


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(db: DbSession, _: CurrentUser, customer_id: int, data: CustomerUpdate):
    customer = _get(db, customer_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    db.commit()
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(db: DbSession, _: AdminUser, customer_id: int):
    customer = _get(db, customer_id)
    if db.scalar(select(Order.id).where(Order.customer_id == customer_id).limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Customer has orders; it can't be deleted")
    db.delete(customer)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
