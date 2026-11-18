from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, CurrentUser, DbSession, Paginate
from app.models import Product, StockMovement, StockReason
from app.schemas import Page, ProductCreate, ProductOut, ProductUpdate, StockAdjust

router = APIRouter(prefix="/products", tags=["products"])


def _get(db, product_id: int, lock: bool = False) -> Product:
    product = db.get(Product, product_id, with_for_update=lock)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.get("", response_model=Page[ProductOut])
def list_products(
    db: DbSession,
    _: CurrentUser,
    page: Paginate,
    q: str | None = None,
    include_inactive: bool = False,
    low_stock: int | None = None,
):
    """List products. `q` searches name and SKU; `low_stock=5` lists items with 5 or fewer left."""
    query = select(Product)
    if not include_inactive:
        query = query.where(Product.is_active)
    if q:
        query = query.where(or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%")))
    if low_stock is not None:
        query = query.where(Product.stock <= low_stock)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Product.name).limit(page.limit).offset(page.offset))
    return Page(items=list(items), total=total, limit=page.limit, offset=page.offset)


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(db: DbSession, admin: AdminUser, data: ProductCreate):
    if db.scalar(select(Product).where(Product.sku == data.sku)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"SKU '{data.sku}' already exists")
    product = Product(**data.model_dump())
    db.add(product)
    db.flush()
    if product.stock:
        db.add(
            StockMovement(
                product_id=product.id,
                delta=product.stock,
                reason=StockReason.initial,
                user_id=admin.id,
            )
        )
    db.commit()
    return product


@router.get("/{product_id}", response_model=ProductOut)
def get_product(db: DbSession, _: CurrentUser, product_id: int):
    return _get(db, product_id)


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(db: DbSession, _: AdminUser, product_id: int, data: ProductUpdate):
    """Edit a product. Past invoices keep the price they were sold at."""
    product = _get(db, product_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    return product


@router.delete("/{product_id}", response_model=ProductOut)
def archive_product(db: DbSession, _: AdminUser, product_id: int):
    """Take a product off sale. It stays in the database so old orders still reference it."""
    product = _get(db, product_id)
    product.is_active = False
    db.commit()
    return product


@router.post("/{product_id}/stock", response_model=ProductOut)
def adjust_stock(db: DbSession, admin: AdminUser, product_id: int, data: StockAdjust):
    """Add a delivery (positive delta) or record a loss (negative delta)."""
    product = _get(db, product_id, lock=True)
    if data.delta == 0:
        raise HTTPException(422, "delta can't be 0")
    if product.stock + data.delta < 0:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only {product.stock} in stock")
    product.stock += data.delta
    db.add(
        StockMovement(
            product_id=product.id,
            delta=data.delta,
            reason=StockReason.adjustment,
            note=data.note,
            user_id=admin.id,
        )
    )
    db.commit()
    return product
